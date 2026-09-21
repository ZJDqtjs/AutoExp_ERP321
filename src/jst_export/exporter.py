"""聚水潭销售出库单导出。

实测流程（对应 HAR 中的请求链）：
  1. GET  /app/wms/saleout/saleout.aspx            -> 取 __VIEWSTATE / __VIEWSTATEGENERATOR
  2. POST /app/wms/saleout/saleout.aspx?am___=ExportSaleOut
                                                   -> 返回导出令牌 JTable:<uid>-<hash>
  3. GET  /app/wms/saleout/ExportSaleoutV2.aspx?s=<令牌>
                                                   -> 302 到 www-do.erp321.com
  4. GET  www-do.erp321.com/.../ExportSaleoutV2.aspx?s=<令牌>
                                                   -> 302 到 OSS 签名地址（xlsx）
  5. GET  OSS 地址                                  -> 真正的 Excel 文件
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .client import BASE_URL, JstSession, NotLoggedIn
from .config import Config, save_cookie
from .login import build_relogin

log = logging.getLogger(__name__)

SALEOUT_PATH = "/app/wms/saleout/saleout.aspx"
EXPORT_PAGE_PATH = "/app/wms/saleout/ExportSaleoutV2.aspx"
PAGE_QUERY = "?_c=jst-epaas&epaas=true"

_VIEWSTATE_RE = re.compile(r'id="__VIEWSTATE" value="([^"]*)"')
_VIEWGEN_RE = re.compile(r'id="__VIEWSTATEGENERATOR" value="([^"]*)"')
_TOKEN_RE = re.compile(r'"ReturnValue":"(JTable:[^"]+)"')
_MOVED_RE = re.compile(r'href="([^"]+)"')
_TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 基础表单字段，取自 HAR 中 ExportSaleOut 的真实请求体
_BASE_FIELDS = {
    "set_type": "1",
    "l_id2": "", "l_id4": "", "sorted_ioids": "", "is_tpw_wms": "0", "insurePrice": "",
    "o_id": "", "sku_id": "", "lc_ids": "", "lc_id_dom": "", "status": "", "is_weight": "",
    "labels": "", "nolabels": "", "seller_flag": "", "node": "", "is_print_express": "",
    "is_print": "", "shop_name": "", "receiver_nameCtrl": "", "remark": "", "outer_so_id": "",
    "platformId": "", "print_count": "", "drp_co_id_id": "", "drp_co_id": "", "sku_count": "",
    "sWeight": "", "bWeight": "", "receiver_state": "", "carryId": "", "wms_co_id_select_id": "",
    "wms_co_id_select": "", "cus_id": "", "cus": "", "creator": "", "goodType": "skus",
    "has_ipc": "", "_jt_page_count_enabled": "", "_jt_page_increament_enabled": "true",
    "_jt_page_increament_page_mode": "", "_jt_page_increament_key_value": "",
    "_jt_page_increament_business_values": "", "_jt_page_increament_key_name": "io_id",
    "_jt_page_size": "500", "receiver_city": "", "receiver_district": "", "receiver_address": "",
    "receiver_name": "", "receiver_phone": "", "receiver_mobile": "", "check_name": "",
    "check_address": "", "_cbb_lc_id_dom": "", "_cbb_status": "", "_cbb_shop_name": "",
    "_cbb_receiver_state": "", "_cbb_wms_co_id_select": "", "_cbb_cus": "",
    "__CALLBACKID": "JTable1",
}


class ExportError(RuntimeError):
    pass


def make_relogin(cfg: Config):
    """Cookie 失效时用账号密码换一套新的，并写回 .env 供下次启动复用。

    未配置账号密码时返回 None，此时 Cookie 失效只能人工处理。
    """
    base = build_relogin(cfg.account, cfg.password, cfg.cookie, timeout=cfg.timeout)
    if base is None:
        return None

    def _relogin() -> str:
        cookie = base()
        save_cookie(cookie)
        cfg.cookie = cookie
        log.info("新 Cookie 已写回 .env")
        return cookie

    return _relogin


class Exporter:
    def __init__(self, cfg: Config, session: JstSession | None = None):
        self.cfg = cfg
        if session is not None:
            self._session = session
            self._owns_session = False
        else:
            self._session = JstSession(
                cfg.cookie, cfg.min_interval, cfg.timeout, relogin=make_relogin(cfg)
            )
            self._owns_session = True

    def __enter__(self) -> "Exporter":
        return self

    def __exit__(self, *exc) -> None:
        if self._owns_session:
            self._session.close()

    # ---------- 步骤 1：取表单隐藏字段 ----------
    def _load_viewstate(self) -> tuple[str, str]:
        resp = self._session.get(BASE_URL + SALEOUT_PATH + PAGE_QUERY)
        JstSession.ensure_logged_in(resp)
        resp.raise_for_status()
        vs = _VIEWSTATE_RE.search(resp.text)
        vg = _VIEWGEN_RE.search(resp.text)
        if not vs or not vg:
            raise ExportError("未能从出库单页面解析出 __VIEWSTATE，页面结构可能已变更")
        return vs.group(1), vg.group(1)

    # ---------- 步骤 2：创建导出（返回导出令牌） ----------
    def create_export_task(self, start: datetime, end: datetime) -> str:
        viewstate, viewgen = self._load_viewstate()

        filters = [
            {"k": self.cfg.io_date_field, "v": start.strftime(_TIME_FMT), "c": ">="},
            {"k": self.cfg.io_date_field, "v": end.strftime(_TIME_FMT), "c": "<"},
        ]
        export_par = {
            "Filter": json.dumps(filters, separators=(",", ":")),
            "CheckIoIds": [],
            "Flag": self.cfg.flag,
            "CheckoutAfterSend": [],
            "IsExportSonInouts": False,
            "IsExportNoTask": True,
        }
        callback = {
            "Method": "ExportSaleOut",
            "Args": [json.dumps(export_par, separators=(",", ":"))],
            "CallControl": "{page}",
        }

        data = dict(_BASE_FIELDS)
        data.update({
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewgen,
            "owner_co_id": self.cfg.owner_co_id,
            "authorize_co_id": self.cfg.authorize_co_id,
            "__CALLBACKPARAM": json.dumps(callback, separators=(",", ":")),
        })

        url = f"{BASE_URL}{SALEOUT_PATH}{PAGE_QUERY}&ts___={int(time.time() * 1000)}&am___=ExportSaleOut"
        resp = self._session.post(url, data=data)
        JstSession.ensure_logged_in(resp)
        resp.raise_for_status()

        if '"IsSuccess":false' in resp.text:
            raise ExportError(f"创建导出任务失败：{resp.text[:300]}")
        match = _TOKEN_RE.search(resp.text)
        if not match:
            raise ExportError(f"未能解析导出令牌，返回内容：{resp.text[:300]}")
        token = match.group(1)
        log.info("导出任务已创建，令牌 %s（区间 %s ~ %s）", token, start, end)
        return token

    # ---------- 步骤 3~5：取文件 ----------
    def fetch_file(self, token: str) -> tuple[bytes, str]:
        params = {
            "s": token,
            "owner_co_id": self.cfg.owner_co_id,
            "authorize_co_id": self.cfg.authorize_co_id,
        }
        url = BASE_URL + EXPORT_PAGE_PATH
        resp = self._session.get(url, params=params)

        # 首次可能直接 302，也可能先返回"正在生成"的页面
        for attempt in range(1, self.cfg.max_retries + 1):
            JstSession.ensure_logged_in(resp, redirect_ok=True)
            target = self._extract_redirect(resp)
            if target:
                break
            log.info("导出文件尚未生成，%ds 后重试（%d/%d）", int(self.cfg.min_interval), attempt, self.cfg.max_retries)
            if attempt == self.cfg.max_retries:
                raise ExportError(f"导出文件生成超时，最后一次响应：{resp.text[:300]}")
            time.sleep(self.cfg.min_interval)
            resp = self._session.get(url, params=params)

        log.info("导出文件地址已下发，开始下载")
        download = self._session.get(
            target,
            headers={"referer": BASE_URL + SALEOUT_PATH + PAGE_QUERY},
            follow_redirects=True,
        )
        JstSession.ensure_logged_in(download, redirect_ok=True)
        download.raise_for_status()

        content_type = download.headers.get("content-type", "")
        if "html" in content_type and not download.content.startswith(b"PK"):
            raise ExportError(f"下载到的不是 Excel 文件：{download.text[:300]}")

        filename = self._extract_filename(download, target)
        return download.content, filename

    @staticmethod
    def _extract_redirect(resp: httpx.Response) -> str | None:
        location = resp.headers.get("location")
        if location:
            return location
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            match = _MOVED_RE.search(resp.text)
            if match:
                return match.group(1).replace("&amp;", "&")
        return None

    @staticmethod
    def _extract_filename(resp: httpx.Response, fallback_url: str) -> str:
        disposition = resp.headers.get("content-disposition", "")
        match = re.search(r"filename=([^;]+)", disposition)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        return unquote(Path(urlparse(fallback_url).path).name)

    # ---------- 对外入口 ----------
    def export(self, start: datetime, end: datetime) -> Path:
        token = self.create_export_task(start, end)
        content, remote_name = self.fetch_file(token)

        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        name = remote_name if self.cfg.keep_raw_name else self.cfg.filename_template.format(
            start=start, end=end, authorize_co_id=self.cfg.authorize_co_id
        )
        path = self.cfg.output_dir / name
        path.write_bytes(content)
        log.info("已保存 %s（%.1f KB）", path, len(content) / 1024)
        return path

    def export_with_retries(self, start: datetime, end: datetime) -> Path:
        last_error: Exception | None = None
        relogged = False  # 同一次导出最多续登一次，避免拿新 Cookie 反复登录
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                return self.export(start, end)
            except NotLoggedIn as exc:
                last_error = exc
                if relogged:
                    raise
                if not self._session.relogin():
                    raise
                relogged = True
                log.warning("Cookie 已失效并完成自动续登，重试导出")
            except Exception as exc:  # noqa: BLE001 - 网络抖动等一律重试
                last_error = exc
                log.warning("第 %d/%d 次导出失败：%s", attempt, self.cfg.max_retries, exc)
                if attempt < self.cfg.max_retries:
                    time.sleep(self.cfg.min_interval)
        raise ExportError(f"导出失败，已重试 {self.cfg.max_retries} 次：{last_error}")