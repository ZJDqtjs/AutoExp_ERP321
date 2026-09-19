"""账号密码自动登录 + 分仓列表查询。

登录接口来自登录页 SPA（erp-login-react）逆向：
    POST https://api.erp321.com/erp/webapi/UserApi/WebLogin/Passport
    请求体 {"data": {"account", "password", "verifyCode", "j_d_3", "v_d_144", "isApp"}}
    返回体 code=0 表示成功，同时通过 Set-Cookie 下发整套登录态。

分仓列表由 saleout.aspx 服务端直出，内嵌在 `var warehouseList = [{...}]` 中。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from .client import BASE_URL, USER_AGENT

log = logging.getLogger(__name__)

LOGIN_PAGE_URL = f"{BASE_URL}/login.aspx"
PASSPORT_URL = "https://api.erp321.com/erp/webapi/UserApi/WebLogin/Passport"

_WAREHOUSE_RE = re.compile(r'var\s+warehouseList\s*=\s*(\[\{.*?\}\])', re.S)
_ITEM_RE = re.compile(r'\{"value":(\d+),"text":"(.*?)"\}')

# 登录返回码 -> 人话
_CODE_MSG = {
    0: "登录成功",
    10003: "触发了图形验证码校验，纯请求登录无法通过",
    10005: "账号或密码错误",
    301105: "需要安全校验（uidSecuCode 已过期）",
    301109: "触发了 IDAAS 二次验证，需要人工处理",
}

_HEADERS = {
    "content-type": "application/json",
    "origin": "https://src.erp321.com",
    "referer": "https://src.erp321.com/",
    "accept": "application/json, text/plain, */*",
    "user-agent": USER_AGENT,
}


class LoginError(RuntimeError):
    """登录失败（账号密码错误、被风控拦截等）。"""


@dataclass(frozen=True)
class Warehouse:
    co_id: str
    name: str

    def __str__(self) -> str:
        return f"{self.co_id}\t{self.name}"


def cookie_from_jar(jar, *, device_id: str | None = None, client_id: str | None = None) -> str:
    """把 CookieJar 拼成请求头字符串，并补上设备指纹字段。"""
    parts: dict[str, str] = {}
    for ck in jar:
        parts[ck.name] = ck.value
    if device_id and not parts.get("j_d_3"):
        parts["j_d_3"] = device_id
    if client_id and not parts.get("v_d_144"):
        parts["v_d_144"] = client_id
    return "; ".join(f"{k}={v}" for k, v in parts.items())


def login(
    account: str,
    password: str,
    *,
    device_id: str | None = None,
    client_id: str | None = None,
    timeout: float = 60.0,
) -> str:
    """用账号密码换一套新的 Cookie，失败抛 LoginError。

    device_id / client_id 对应浏览器的 j_d_3 / v_d_144 设备指纹；
    实测缺失或随机值都能登录成功，传进来只是让新 Cookie 与旧的一致。
    """
    if not account or not password:
        raise LoginError("未配置账号或密码，无法自动登录")

    payload = {
        "data": {
            "account": account,
            "password": password,
            "verifyCode": "",
            "j_d_3": device_id or "",
            "v_d_144": client_id or "",
            "isApp": False,
        }
    }

    with httpx.Client(headers={"user-agent": USER_AGENT}, follow_redirects=False, timeout=timeout) as client:
        # 先落一个 acw_tc 会话票据，再调登录接口
        client.get(LOGIN_PAGE_URL)
        resp = client.post(PASSPORT_URL, json=payload, headers=_HEADERS)
        resp.raise_for_status()
        body = resp.json()

        code = body.get("code")
        if code != 0:
            raise LoginError(f"登录失败（code={code}）：{_CODE_MSG.get(code, body.get('msg') or '未知错误')}")

        data = body.get("data") or {}
        if data.get("hasRisk") or data.get("idaasHasRisk"):
            raise LoginError(f"登录被风控拦截（hasRisk={data.get('hasRisk')}），需要人工在浏览器登录")

        cookie = cookie_from_jar(client.cookies.jar, device_id=device_id, client_id=client_id)

    if "isLogin=true" not in cookie:
        raise LoginError("登录接口返回成功，但未下发登录态 Cookie")
    log.info("登录成功，账号 %s", account)
    return cookie


def parse_warehouses(html: str) -> list[Warehouse]:
    """从 saleout.aspx 页面中解析分仓列表。"""
    match = _WAREHOUSE_RE.search(html)
    if not match:
        return []
    return [Warehouse(co_id, name) for co_id, name in _ITEM_RE.findall(match.group(1))]


def device_id_from_cookie(cookie: str) -> tuple[str | None, str | None]:
    """从现有 Cookie 里取出 j_d_3 / v_d_144，续登时保持一致。"""
    fields = {}
    for item in cookie.split(";"):
        key, _, value = item.strip().partition("=")
        if key:
            fields[key] = value
    return fields.get("j_d_3") or None, fields.get("v_d_144") or None


def build_relogin(account: str, password: str, old_cookie: str = "", *, timeout: float = 60.0):
    """生成给 JstSession 用的续登回调；未配置账号密码时返回 None。"""
    if not account or not password:
        return None

    device_id, client_id = device_id_from_cookie(old_cookie)

    def _relogin() -> str:
        return login(account, password, device_id=device_id, client_id=client_id, timeout=timeout)

    return _relogin