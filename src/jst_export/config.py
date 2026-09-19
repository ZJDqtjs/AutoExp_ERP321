"""配置加载：从 .env / 环境变量读取。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

WINDOW_YESTERDAY = "yesterday"
WINDOW_TODAY = "today"
WINDOW_LAST24H = "last24h"

_COOKIE_LINE_RE = re.compile(r"^JST_COOKIE\s*=.*$", re.M)
_RE_DAY_AGO = re.compile(r"^d(\d+)$")  # d1=昨天, d2=前天
_RE_LAST_DAYS = re.compile(r"^last(\d+)d$")  # last7d=最近 7 个整天
_RE_LAST_HOURS = re.compile(r"^last(\d+)h$")  # last2h=最近 2 小时（含 last24h）

WINDOW_HELP = (
    "可用的区间规则：\n"
    "  today            今天 00:00 ~ 明天 00:00\n"
    "  yesterday / d1   昨天 00:00 ~ 今天 00:00\n"
    "  dN               第 N 天前那一整天，如 d2=前天、d3=大前天\n"
    "  lastNd           最近 N 个整天，如 last7d=今天往前 7 天\n"
    "  lastNh           最近 N 小时（向上取整到整点），如 last2h、last24h"
)


def resolve_window(window: str, now: datetime) -> tuple[datetime, datetime]:
    """把区间规则解析成 [start, end)（左闭右开）。"""
    window = (window or WINDOW_YESTERDAY).strip().lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if window == WINDOW_TODAY:
        return today, today + timedelta(days=1)
    if window in (WINDOW_YESTERDAY, "d1"):
        return today - timedelta(days=1), today

    match = _RE_DAY_AGO.match(window)
    if match:
        days = int(match.group(1))
        if days < 1:
            raise ValueError(f"区间规则 {window!r} 无效：N 必须 >= 1\n{WINDOW_HELP}")
        end = today - timedelta(days=days - 1)
        return end - timedelta(days=1), end

    match = _RE_LAST_DAYS.match(window)
    if match:
        days = int(match.group(1))
        if days < 1:
            raise ValueError(f"区间规则 {window!r} 无效：N 必须 >= 1\n{WINDOW_HELP}")
        return today - timedelta(days=days), today

    match = _RE_LAST_HOURS.match(window)
    if match:
        hours = int(match.group(1))
        if hours < 1:
            raise ValueError(f"区间规则 {window!r} 无效：N 必须 >= 1\n{WINDOW_HELP}")
        # 结束点向上取整到下一个整点，保证同一小时内多次运行结果稳定
        end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return end - timedelta(hours=hours), end

    raise ValueError(f"无法识别的区间规则 {window!r}\n{WINDOW_HELP}")


@dataclass(frozen=True, order=True)
class Schedule:
    """一个定时执行点。window 为空表示用 JST_WINDOW。"""

    hour: int
    minute: int
    window: str = ""

    def __str__(self) -> str:
        base = f"{self.hour:02d}:{self.minute:02d}"
        return f"{base}({self.window})" if self.window else base


@dataclass
class Config:
    cookie: str
    owner_co_id: str
    authorize_co_id: str
    output_dir: Path
    window: str = WINDOW_YESTERDAY
    account: str = ""
    password: str = ""
    fixed_range: tuple[datetime, datetime] | None = None
    io_date_field: str = "io_date"
    min_interval: float = 10.0
    timeout: float = 120.0
    max_retries: int = 3
    flag: int = 0
    schedule: list[Schedule] = field(default_factory=list)
    filename_template: str = "销售出库单_{start:%Y%m%d}.xlsx"
    keep_raw_name: bool = False

    @property
    def can_relogin(self) -> bool:
        return bool(self.account and self.password)

    def resolve_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """按 JST_WINDOW 规则算出本次要导出的 [start, end) 时间段。"""
        return resolve_window(self.window, now or datetime.now())


def _parse_schedule(raw: str) -> list[Schedule]:
    """解析 "07:30,19:30:today" 形式的调度时间，每项可单独指定区间规则。"""
    slots: list[Schedule] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError(f"无法解析调度时间 {item!r}，应为 HH:MM 或 HH:MM:区间规则")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"调度时间超出范围: {item!r}")
        window = parts[2].strip().lower() if len(parts) > 2 else ""
        if window:
            resolve_window(window, datetime.now())  # 提前校验规则是否合法
        slots.append(Schedule(hour, minute, window))
    return sorted(set(slots))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_datetime(raw: str) -> datetime:
    """解析 2026-09-18 / 2026-09-18 08:00 / 2026-09-18 08:00:00。"""
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间 {raw!r}，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]")


def _parse_fixed_range() -> tuple[datetime, datetime] | None:
    """JST_START / JST_END：在 .env 里写死一段区间，适合一次性补数据。"""
    raw_start = (os.getenv("JST_START") or "").strip()
    raw_end = (os.getenv("JST_END") or "").strip()
    if not raw_start and not raw_end:
        return None
    if not (raw_start and raw_end):
        raise RuntimeError("JST_START 与 JST_END 必须同时配置")
    start, end = parse_datetime(raw_start), parse_datetime(raw_end)
    if start >= end:
        raise RuntimeError(f"JST_START({start}) 必须早于 JST_END({end})")
    return start, end


def save_cookie(cookie: str, env_file: Path | None = None) -> Path:
    """把新登录得到的 Cookie 写回 .env，下次启动直接复用。"""
    path = env_file or PROJECT_ROOT / ".env"
    line = f'JST_COOKIE="{cookie}"'
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _COOKIE_LINE_RE.search(text):
        text = _COOKIE_LINE_RE.sub(lambda _: line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or PROJECT_ROOT / ".env")

    cookie = (os.getenv("JST_COOKIE") or "").strip()
    account = (os.getenv("JST_ACCOUNT") or "").strip()
    password = (os.getenv("JST_PASSWORD") or "").strip()
    if not cookie and not (account and password):
        raise RuntimeError(
            "未配置 JST_COOKIE；也不想自动登录的话请在 .env 中填入 JST_ACCOUNT / JST_PASSWORD"
        )

    owner = (os.getenv("JST_OWNER_CO_ID") or "13662884").strip()
    authorize = (os.getenv("JST_AUTHORIZE_CO_ID") or owner).strip()

    output_dir = Path(os.getenv("JST_OUTPUT_DIR") or PROJECT_ROOT / "exports")
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir

    window = (os.getenv("JST_WINDOW") or WINDOW_YESTERDAY).strip().lower()
    resolve_window(window, datetime.now())  # 提前校验规则是否合法

    return Config(
        cookie=cookie,
        owner_co_id=owner,
        authorize_co_id=authorize,
        output_dir=output_dir,
        window=window,
        account=account,
        password=password,
        fixed_range=_parse_fixed_range(),
        io_date_field=(os.getenv("JST_IO_DATE_FIELD") or "io_date").strip(),
        min_interval=float(os.getenv("JST_MIN_INTERVAL") or 10),
        timeout=float(os.getenv("JST_TIMEOUT") or 120),
        max_retries=int(os.getenv("JST_MAX_RETRIES") or 3),
        flag=int(os.getenv("JST_FLAG") or 0),
        schedule=_parse_schedule(os.getenv("JST_SCHEDULE") or ""),
        filename_template=(os.getenv("JST_FILENAME_TEMPLATE") or "销售出库单_{start:%Y%m%d}.xlsx"),
        keep_raw_name=_env_bool("JST_KEEP_RAW_NAME"),
    )