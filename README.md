# AutoExp_ERP321

聚水潭 ERP（erp321.com）销售出库单定时自动导出工具。

纯 HTTP 请求实现，无需浏览器：到点自动创建导出任务、拉取 Excel、落盘到本地目录。
Cookie 失效时用账号密码自动续登，并把新 Cookie 写回 `.env`。

## 功能

- **定时导出**：常驻进程按配置的时间点自动导出，每个时间点可指定不同的时间区间
- **灵活的时间区间**：`yesterday` / `today` / `dN`（第 N 天前）/ `lastNd`（最近 N 天）/ `lastNh`（最近 N 小时），也可命令行或配置里写死任意区间
- **自动续登**：Cookie 过期时自动用账号密码重新登录，无需人工干预
- **分仓切换**：一条命令列出账号下全部分仓编码，改配置即可切换导出哪个仓
- **限速保护**：两次请求间隔不低于 10 秒，避免触发风控

## 快速开始

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

复制 `.env.example` 的字段到 `.env`（或直接手写），至少填上账号密码：

```ini
JST_ACCOUNT=你的登录手机号
JST_PASSWORD=你的登录密码
JST_OWNER_CO_ID=13662884
JST_AUTHORIZE_CO_ID=14975440
```

首次登录并验证：

```bash
uv run jst-export check      # 确认 .env 里的 Cookie 可用
uv run jst-export warehouses # 列出所有分仓编码与名称
```

`.env` 里没有 Cookie 时会自动登录一次；之后一律复用已保存的 Cookie，
只有真的失效（被踢回登录页 / 返回 `GotoLogin`）才会重新登录，不会每次拉取都登一遍。

导出一次：

```bash
uv run jst-export run                      # 按 JST_WINDOW 规则（默认昨天）
uv run jst-export run --window last7d      # 最近 7 天
uv run jst-export run --start 2026-09-01 --end 2026-09-10

# 临时导出别的分仓，并把仓号写进文件名，避免多仓互相覆盖
uv run jst-export run --start 2026-09-20 --end 2026-09-21 \
  -w 14975440 --filename-template "销售出库单_{start:%Y%m%d}_{authorize_co_id}.xlsx"
```

常驻定时：

```bash
uv run jst-export daemon
```

## 命令说明

| 命令 | 作用 |
| --- | --- |
| `run` | 立即导出一次，可用 `--start/--end`、`--window`、`-w/--warehouse`、`--filename-template` 覆盖配置 |
| `daemon` | 常驻进程，按 `JST_SCHEDULE` 定时导出 |
| `check` | 检查当前 Cookie 是否有效 |
| `login` | 用配置的账号密码登录，刷新 `.env` 里的 Cookie |
| `warehouses` | 列出全部分仓编码（`authorize_co_id`）与名称 |

## 时间区间规则

区间统一为**左闭右开** `[start, end)`：含开始时间，不含结束时间。

假设当前时间为 `2026-09-19 16:20`：

| 规则 | 实际区间 | 说明 |
| --- | --- | --- |
| `today` | 09-19 00:00 ~ 09-20 00:00 | 今天全天 |
| `yesterday` / `d1` | 09-18 00:00 ~ 09-19 00:00 | 昨天一整天（默认） |
| `d2` | 09-17 00:00 ~ 09-18 00:00 | 前天那一整天 |
| `last7d` | 09-12 00:00 ~ 09-19 00:00 | 最近 7 个整天 |
| `last24h` | 09-18 17:00 ~ 09-19 17:00 | 最近 24 小时，结束点向上取整到整点 |

规则写错会直接报错并列出所有可用写法。

### 每个定时点使用不同区间

```ini
# 早上导昨天、晚上导今天
JST_SCHEDULE=07:30:yesterday,19:30:today
```

不标注区间的项沿用 `JST_WINDOW`。

### 写死固定区间

适合一次性补历史数据，**用完记得注释掉**，否则 `daemon` 会每天重复导出同一段：

```ini
JST_START=2026-09-01
JST_END=2026-09-10
```

### 优先级

```
命令行 --start/--end  >  --window  >  JST_START/JST_END  >  JST_WINDOW
```

## 配置项

全部配置放在项目根目录的 `.env`。完整注释见文件本身。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `JST_ACCOUNT` | — | 登录手机号，用于 Cookie 失效时自动续登 |
| `JST_PASSWORD` | — | 登录密码 |
| `JST_COOKIE` | — | 登录态，由 `login` 或自动续登写入，无需手填 |
| `JST_OWNER_CO_ID` | `13662884` | 登录公司主体 |
| `JST_AUTHORIZE_CO_ID` | 同 owner | 要导出的分仓编码 |
| `JST_WINDOW` | `yesterday` | 默认区间规则 |
| `JST_START` / `JST_END` | 空 | 写死的固定区间，两项必须同时填 |
| `JST_SCHEDULE` | 空 | 定时点，如 `07:30,19:30:today` |
| `JST_OUTPUT_DIR` | `exports` | 导出目录 |
| `JST_MIN_INTERVAL` | `10` | 两次请求最小间隔（秒） |
| `JST_TIMEOUT` | `120` | 单次请求超时（秒） |
| `JST_MAX_RETRIES` | `3` | 导出重试次数 |
| `JST_FILENAME_TEMPLATE` | `销售出库单_{start:%Y%m%d}.xlsx` | 文件名模板，支持 `{start}` `{end}` `{authorize_co_id}` |

## 工作原理

导出流程对应抓包得到的 5 步请求链：

1. `GET /app/wms/saleout/saleout.aspx` —— 取 `__VIEWSTATE` / `__VIEWSTATEGENERATOR`
2. `POST` 同一路径（`am___=ExportSaleOut`）—— 提交过滤条件，返回导出令牌 `JTable:<uid>-<hash>`
3. `GET /app/wms/saleout/ExportSaleoutV2.aspx?s=<令牌>` —— 302 到 `www-do.erp321.com`
4. `GET www-do.erp321.com/.../ExportSaleoutV2.aspx?s=<令牌>` —— 302 到 OSS 签名地址
5. `GET OSS 地址` —— 下载真正的 xlsx 字节流

登录走 `POST https://api.erp321.com/erp/webapi/UserApi/WebLogin/Passport`，
返回码 `0` 为成功，同时通过 `Set-Cookie` 下发整套登录态。

分仓列表由 `saleout.aspx` 服务端直出，内嵌在 `var warehouseList = [...]` 中。

## 项目结构

```
src/jst_export/
  client.py     带限速的 HTTP 会话，支持 Cookie 失效自动续登
  config.py     .env 加载、时间区间规则解析、Cookie 写回
  login.py      账号密码登录 + 分仓列表解析
  exporter.py   核心 5 步导出流程
  __main__.py   命令行入口（run / daemon / check / login / warehouses）
```

## 注意事项

- **`.env` 含明文密码，已在 `.gitignore` 中排除，请勿提交或外传**
- 聚水潭有风控，`JST_MIN_INTERVAL` 不建议低于 10 秒
- 若登录返回 `10003` 或 `301109`，说明触发了图形验证码或二次验证，此时需要在浏览器手动登录后把 Cookie 填进 `.env`
- `--end` 是**排他上界**：`--end 2026-09-10` 表示导出到 9 月 9 日为止，不含 9 月 10 日