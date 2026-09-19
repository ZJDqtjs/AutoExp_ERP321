"""带限速的 HTTP 会话。

聚水潭对高频请求会做风控，这里统一保证两次请求之间至少间隔 min_interval 秒。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://www.erp321.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)


class NotLoggedIn(RuntimeError):
    """Cookie 失效 / 未登录。"""


class JstSession:
    def __init__(
        self,
        cookie: str,
        min_interval: float = 10.0,
        timeout: float = 120.0,
        relogin: Callable[[], str] | None = None,
    ):
        self._min_interval = min_interval
        self._last_request_at = 0.0
        self._lock = threading.Lock()
        self._relogin = relogin
        if not cookie and relogin:
            cookie = relogin()
        self._client = httpx.Client(
            headers={
                "accept": "*/*",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                "origin": BASE_URL,
                "user-agent": USER_AGENT,
                "x-requested-with": "XMLHttpRequest",
                "cookie": cookie,
            },
            timeout=timeout,
            follow_redirects=False,
        )

    def __enter__(self) -> "JstSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def cookie(self) -> str:
        return self._client.headers.get("cookie", "")

    def relogin(self) -> bool:
        """Cookie 失效时重新登录并就地替换请求头，返回是否续登成功。"""
        if self._relogin is None:
            return False
        log.warning("登录态失效，正在自动重新登录…")
        self._client.headers["cookie"] = self._relogin()
        return True

    def _throttle(self) -> None:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                log.debug("限速等待 %.1fs", wait)
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self._throttle()
        log.debug("%s %s", method, url)
        return self._client.request(method, url, **kwargs)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    @staticmethod
    def ensure_logged_in(resp: httpx.Response, *, redirect_ok: bool = False) -> None:
        """识别被踢回登录页的情况，避免拿着失效 Cookie 反复重试。"""
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if "login" in location.lower() or "/epaas" == location.rstrip("/"):
                raise NotLoggedIn("Cookie 已失效，被重定向到登录页，请重新获取 JST_COOKIE")
            if redirect_ok:
                return
        if resp.status_code in (401, 403):
            raise NotLoggedIn(f"请求被拒绝（HTTP {resp.status_code}），Cookie 可能已失效")
        if '"GotoLogin":true' in resp.text:
            raise NotLoggedIn("接口返回 GotoLogin=true，Cookie 已失效，请重新获取 JST_COOKIE")