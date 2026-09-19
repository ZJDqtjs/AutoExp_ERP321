"""命令行入口：立即导出 / 常驻定时导出 / 检查登录态。"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from .client import BASE_URL, JstSession, NotLoggedIn
from .config import Config, WINDOW_HELP, load_config, parse_datetime, resolve_window, save_cookie
from .exporter import ExportError, Exporter, make_relogin
from .login import LoginError, login, parse_warehouses

log = logging.getLogger("jst_export")

SALEOUT_URL = f"{BASE_URL}/app/wms/saleout/saleout.aspx?_c=jst-epaas&epaas=true"


def _parse_datetime(raw: str) -> datetime:
    try:
        return parse_datetime(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_window(raw: str) -> str:
    try:
        resolve_window(raw, datetime.now())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return raw


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _resolve_range(cfg: Config, args: argparse.Namespace) -> tuple[datetime, datetime]:
    """优先级：命令行 --start/--end > --window > .env 的 JST_START/JST_END > JST_WINDOW。"""
    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("--start 与 --end 必须同时提供")
        return args.start, args.end
    if args.window:
        return resolve_window(args.window, datetime.now())
    if cfg.fixed_range:
        return cfg.fixed_range
    return cfg.resolve_window()


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    start, end = _resolve_range(cfg, args)
    with Exporter(cfg) as exporter:
        path = exporter.export_with_retries(start, end)
    print(f"导出完成：{path}")
    return 0


def cmd_daemon(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.schedule:
        raise SystemExit("未配置 JST_SCHEDULE，例如 JST_SCHEDULE=07:30,19:30:today")
    plan = "，".join(str(s) for s in cfg.schedule)
    log.info("定时导出已启动，每天执行：%s（未标注区间的用 JST_WINDOW=%s）", plan, cfg.window)
    if cfg.fixed_range:
        log.warning(
            "检测到 JST_START/JST_END=%s ~ %s，将每天重复导出固定区间",
            *cfg.fixed_range,
        )

    done: set[str] = set()
    with Exporter(cfg) as exporter:
        while True:
            now = datetime.now()
            for slot in cfg.schedule:
                if now.hour != slot.hour or now.minute != slot.minute:
                    continue
                key = f"{now:%Y-%m-%d} {slot.hour:02d}:{slot.minute:02d}"
                if key in done:
                    continue
                done.add(key)
                if cfg.fixed_range:
                    start, end = cfg.fixed_range
                else:
                    start, end = resolve_window(slot.window or cfg.window, now)
                log.info("触发定时导出（%s）：区间 %s ~ %s", slot, start, end)
                try:
                    path = exporter.export_with_retries(start, end)
                    log.info("定时导出完成：%s", path)
                except NotLoggedIn as exc:
                    log.error("登录态失效且无法自动续登，跳过本次：%s", exc)
                except ExportError as exc:
                    log.error("本次导出失败：%s", exc)
            # 清理过期的执行记录，避免集合无限增长
            today = f"{now:%Y-%m-%d}"
            done = {k for k in done if k.startswith(today)}
            time.sleep(20)


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    session = JstSession(cfg.cookie, cfg.min_interval, cfg.timeout)
    try:
        resp = session.get(SALEOUT_URL)
        session.ensure_logged_in(resp)
        if "销售出库单" in resp.text:
            print("Cookie 有效，可以正常访问销售出库单页面")
            return 0
        print(f"Cookie 可用但页面内容异常（HTTP {resp.status_code}）")
        return 1
    except NotLoggedIn as exc:
        print(f"Cookie 已失效：{exc}")
        if cfg.can_relogin:
            print("已配置 JST_ACCOUNT / JST_PASSWORD，运行时会自动重新登录")
        return 1
    finally:
        session.close()


def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    """手动触发一次登录并把新 Cookie 写回 .env。"""
    if not cfg.can_relogin:
        raise SystemExit("未配置 JST_ACCOUNT / JST_PASSWORD，无法自动登录")
    cookie = login(cfg.account, cfg.password, timeout=cfg.timeout)
    path = save_cookie(cookie)
    print(f"登录成功，新 Cookie 已写入 {path}")
    return 0


def cmd_warehouses(cfg: Config, args: argparse.Namespace) -> int:
    """列出当前账号可见的全部分仓编码与名称。"""
    session = JstSession(cfg.cookie, cfg.min_interval, cfg.timeout, relogin=make_relogin(cfg))
    try:
        resp = session.get(SALEOUT_URL)
        session.ensure_logged_in(resp)
        resp.raise_for_status()
        items = parse_warehouses(resp.text)
    finally:
        session.close()

    if not items:
        print("未从页面解析到分仓列表")
        return 1
    print(f"{'分仓编码 (authorize_co_id)':<28}分仓名称")
    print("-" * 60)
    for wh in items:
        mark = "  <- 当前配置" if wh.co_id == cfg.authorize_co_id else ""
        print(f"{wh.co_id:<28}{wh.name}{mark}")
    print(f"\n共 {len(items)} 个，改 .env 里的 JST_AUTHORIZE_CO_ID 即可切换")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jst-export", description="聚水潭销售出库单定时自动导出")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="立即导出一次", epilog=WINDOW_HELP,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("--start", type=_parse_datetime, help="开始时间（含），如 2026-09-18")
    run.add_argument("--end", type=_parse_datetime, help="结束时间（不含），如 2026-09-19")
    run.add_argument(
        "--window",
        type=_parse_window,
        metavar="RULE",
        help="按规则自动计算区间，如 yesterday / today / d2 / last7d / last24h",
    )

    daemon = sub.add_parser("daemon", help="常驻进程，按 JST_SCHEDULE 定时导出")
    daemon.set_defaults(func=cmd_daemon)

    check = sub.add_parser("check", help="检查 Cookie 是否有效")
    check.set_defaults(func=cmd_check)

    login_cmd = sub.add_parser("login", help="用 JST_ACCOUNT / JST_PASSWORD 登录并刷新 .env 里的 Cookie")
    login_cmd.set_defaults(func=cmd_login)

    wh = sub.add_parser("warehouses", help="列出所有分仓编码（authorize_co_id）与名称")
    wh.set_defaults(func=cmd_warehouses)

    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        cfg = load_config()
    except (RuntimeError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        log.info("已手动停止")
        return 130
    except (ExportError, NotLoggedIn, LoginError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())