# -*- coding: utf-8 -*-
"""视频号助手接口的纯 HTTP 直连层（rev50）。

## 职责

全项目**唯一**的 HTTP 出网原语。它是依赖图的叶子节点 —— 不 import 任何项目内模块，
所以 `api_client` / `post_fetcher` / `live_fetcher` / `live_probe_http` 都能安全依赖它，
而不会形成 `live_fetcher -> wx_http -> live_probe_http -> live_fetcher` 的循环。

为什么值得单独抽一层：这个项目里几乎所有网络调用原本都是
`page.evaluate(fetch(url, {credentials:'include'}))`。那是同一件事的浏览器版本 ——
同源、带 cookie、POST JSON。改成进程内 HTTP 只需要三样东西：

1. **cookie** —— config 账号的 `storage_state.cookies`（rev37 起扫码成功就落盘）
2. **`_aid`** —— config 账号的 `_aid`（每次登录刷新并写回）
3. **新鲜 timestamp** —— body 里的毫秒值（服务端不校验历史值，但每次换新的更稳）

rev48 用这套方案做过直播探测（1.1s/次、零浏览器），这里把它泛化成通用层。

## 依赖策略

优先 `httpx`（异步、连接复用）；没装则退回标准库 `urllib` + `asyncio.to_thread`。
用户机器是干净克隆、可能没装 httpx，**不能因为缺一个可选包就整条退化回起浏览器**。

## 边界

`post_json` 返回 `{"__err": why}` 表示"这条路走不通，请调用方回退浏览器"：
  - `no_cookie`：该账号 storage_state 里没有能发给 API host 的 cookie
  - `no_account`：没传账号
  - `request_error`：网络层异常（超时/连接被拒）
  - `http_error`：HTTP >= 400
  - `parse_error`：响应不是 JSON

**绝不在这里凭空造一个"成功但空"的结论** —— 判不了就交回给能判的路径。
"""
import asyncio
import json
import time
from urllib.parse import quote

try:
    import httpx as _httpx
    HAS_HTTPX = True
except Exception:            # 干净克隆的机器可能还没装
    _httpx = None
    HAS_HTTPX = False

DEFAULT_TIMEOUT = 20.0
_API_HOST = "channels.weixin.qq.com"
_ROOT_DOMAIN = "weixin.qq.com"
# 这些 header 由 HTTP 客户端自己管，照抄会出错（长度不对 / 连接归客户端所有）
_SKIP_HEADERS = {"cookie", "content-length", "host", "connection"}

# 与浏览器同源请求近似的默认头。缺 Referer/Origin 时腾讯接口可能返 403 或空体，
# 由调用方按具体页面覆盖 Referer。
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://channels.weixin.qq.com",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
}

_clients = {}          # id(loop) -> AsyncClient（httpx 客户端不能跨事件循环复用）


def _cookie_domain_ok(dom):
    """这个 cookie 的域，浏览器会不会发给 channels.weixin.qq.com？

    必须是精确的「该 host」或「父域泛 cookie」。曾经用 `dom.endswith("weixin.qq.com")`
    过滤，结果把 `developers.weixin.qq.com` 的 bid/bidjs 也带上了，还让 `wxuin`
    出现两次（两个域各一份、值可能不同）—— 重复的同名 cookie 发给服务端是隐患。
    """
    if not dom:
        return False
    if dom == _API_HOST:
        return True
    return dom in (_ROOT_DOMAIN, "." + _ROOT_DOMAIN)


def build_cookie_header(storage_state):
    """storage_state -> `k=v; k=v`。只保留会发给 API host 的 cookie。"""
    out = []
    for c in ((storage_state or {}).get("cookies") or []):
        name = c.get("name")
        val = c.get("value")
        if not name or val is None:
            continue
        if not _cookie_domain_ok((c.get("domain") or "").lower()):
            continue
        out.append("%s=%s" % (name, val))
    return "; ".join(out)


def cookie_header_of(account):
    """账号 dict -> Cookie 头。取不到返回 ""。"""
    return build_cookie_header((account or {}).get("storage_state"))


def std_body(finder_id, **extra):
    """视频号助手接口的公共 body 字段（与官方前端一致）。

    `api_client._body` 和 `post_fetcher._body` 原来各抄了一份，完全同构，这里统一。
    """
    b = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id or "",
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    b.update(extra)
    return b


def api_url(base, path, aid="", page_url=""):
    """拼 `{base}/{path}?_aid=..&_pageUrl=..`。两个 query 都缺省时直接给 base/path。"""
    url = "%s/%s" % (base.rstrip("/"), path.lstrip("/"))
    qs = []
    if aid:
        qs.append("_aid=" + aid)
    if page_url:
        qs.append("_pageUrl=" + quote(page_url, safe=""))
    if qs:
        url += "?" + "&".join(qs)
    return url


# ==================== 传输层 ====================

def _client(timeout):
    """按事件循环复用 AsyncClient —— 每次新建连接池等于放弃 keep-alive。"""
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        return None
    cli = _clients.get(loop_id)
    if cli is None or cli.is_closed:
        cli = _httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        _clients[loop_id] = cli
    return cli


async def _do_httpx(method, url, headers, body, timeout):
    cli = _client(timeout)
    if cli is None:
        raise RuntimeError("no running loop")
    data = body.encode("utf-8") if isinstance(body, str) else body
    r = await cli.request(method, url, headers=headers, content=data)
    return r.status_code, r.text


async def _do_urllib(method, url, headers, body, timeout):
    """标准库兜底。阻塞调用丢进线程，不占事件循环。"""
    import urllib.request
    import urllib.error

    def _call():
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b"").decode("utf-8", "replace")

    return await asyncio.to_thread(_call)


async def request_raw(method, url, headers=None, body=None, timeout=DEFAULT_TIMEOUT):
    """返回 (status_code, text)。httpx 挂了也不要紧，标准库再来一次。"""
    headers = dict(headers or {})
    method = (method or "POST").upper()
    if method == "GET":
        body = None
    if HAS_HTTPX:
        try:
            return await _do_httpx(method, url, headers, body, timeout)
        except Exception:
            pass
    return await _do_urllib(method, url, headers, body, timeout)


def _headers_for(page_url="", extra=None):
    h = dict(BASE_HEADERS)
    if page_url:
        h["Referer"] = page_url
    for k, v in (extra or {}).items():
        if k.lower() in _SKIP_HEADERS:
            continue
        h[k] = v
    return h


# ==================== 业务原语 ====================

async def post_json(account, url, body, page_url="", headers=None,
                    timeout=DEFAULT_TIMEOUT):
    """直发一次 JSON POST，返回解析后的 dict；不可用返回 `{"__err": why}`。

    `body` 传 dict（会 json.dumps）或 str（原样发，给"重放已学规格"用）。
    """
    if not account:
        return {"__err": "no_account"}
    cookie = cookie_header_of(account)
    if not cookie:
        return {"__err": "no_cookie"}
    h = _headers_for(page_url, headers)
    h["Cookie"] = cookie
    payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    try:
        code, txt = await request_raw("POST", url, h, payload, timeout)
    except Exception as e:
        return {"__err": "request_error", "err": repr(e)[:120]}
    if not txt or code >= 400:
        return {"__err": "http_error", "code": code, "raw": (txt or "")[:200]}
    try:
        return json.loads(txt)
    except Exception:
        return {"__err": "parse_error", "code": code, "raw": (txt or "")[:200]}


async def get_bytes(url, referer="", timeout=DEFAULT_TIMEOUT):
    """下载二进制（封面/素材）。CDN 是公开的，不需要 cookie，只带 Referer。"""
    h = {
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": BASE_HEADERS["User-Agent"],
    }
    if referer:
        h["Referer"] = referer
    if HAS_HTTPX:
        try:
            cli = _client(timeout)
            if cli is not None:
                r = await cli.get(url, headers=h)
                return r.content if r.status_code == 200 else None
        except Exception:
            pass
    return await asyncio.to_thread(_get_urllib, url, h, timeout)


def _get_urllib(url, headers, timeout):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if r.status == 200 else None
    except Exception:
        return None


def backend_name():
    """诊断用：当前实际走哪个 HTTP 后端。"""
    return "httpx" if HAS_HTTPX else "urllib"


async def aclose():
    """进程退出前收掉连接池（母本优雅重启时调，避免 socket 泄漏告警）。"""
    for cli in list(_clients.values()):
        try:
            await cli.aclose()
        except Exception:
            pass
    _clients.clear()
