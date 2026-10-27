# -*- coding: utf-8 -*-
"""rev48: 纯 HTTP 直播探测 —— 不启浏览器,一次 POST 拿 check_live_status。

## 为什么可以这么做(2026-10-28 实测)

原来每次探测都要起一个 Chromium:要么开整个直播大屏 SPA(141 秒),要么只建
context 用 `ctx.request` 发一次请求(4~6 秒)。但探测本质上就是**一次同源 POST**,
只要凑齐三样东西,HTTP 层就能直接发:

1. **接口规格** —— `data/live_api_spec.json` 里已经躺着学到的 url(
   含 `_aid`/`_rid`/`_pageUrl`)+ headers(`X-WECHAT-UIN`/`finger-print-device-id`/
   `Referer`)+ postData。
2. **登录态** —— config 账号的 `storage_state.cookies` 里就有
   `sessionid` + `wxuin` @ `channels.weixin.qq.com`。
3. **新鲜 timestamp** —— body 里的 `"timestamp":"<毫秒>"`,服务端不校验历史值,
   但为稳妥起见每次替换成当前毫秒(实测替换后正常返回)。

实测(母本账号,本机直连):`HTTP 201` / `errCode=0` / `1.0s` /
`wxNickname="U.S.POLOASSN.免税仓"` —— 与账号完全对得上,说明这条链路是通的。
**不需要浏览器,不需要 renderer,不需要 cookie 落盘。**

## 依赖策略

优先 `httpx`(异步、连接复用);没装则退回标准库 `urllib` + `asyncio.to_thread`。
两个后端行为一致,都返回 `(status_code, text)`。这么做的原因:用户机器是干净克隆、
可能还没装 httpx,而探测是核心链路,**不能因为缺一个可选包就整条退化回起浏览器**。

## 边界(什么时候返回 None)

返回 None = "这条路走不通,请调用方回退"。三种情况:
  - 该账号还没有 `live_status` 规格(从没开过直播页,没学过) -> 交给完整检测去学
  - `storage_state` 里没有可用 cookie(没登录过/被清空) -> 交给有 context 的路径
  - HTTP 失败 / 响应不可解析 / errCode != 0(sessionid 过期) -> 同上

**绝不在这里凭空造一个"未播"结论** —— 判不了就是判不了,交回给能判的路径。
"""
import asyncio
import json
import os
import re
import time

from .live_fetcher import LiveFetcher, _load_spec_cache, judge_live

try:
    import httpx as _httpx
    HAS_HTTPX = True
except Exception:            # 干净克隆的机器可能还没装
    _httpx = None
    HAS_HTTPX = False

DEFAULT_TIMEOUT = 8.0        # 探测是"看一眼",宁可漏一轮也不要挂住探测器
_API_HOST = "channels.weixin.qq.com"
_ROOT_DOMAIN = "weixin.qq.com"
# 这些 header 由 HTTP 客户端自己管,照抄会出错(长度不对/连接归客户端所有)
_SKIP_HEADERS = {"cookie", "content-length", "host", "connection"}


def _data_dir():
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _cookie_domain_ok(dom):
    """这个 cookie 的域,浏览器会不会发给 channels.weixin.qq.com?

    必须是精确的"该 host"或"父域泛 cookie"。曾经用 `dom.endswith("weixin.qq.com")`
    过滤,结果把 `developers.weixin.qq.com` 的 bid/bidjs 也带上了,还让 `wxuin`
    出现两次(两个域各一份、值可能不同)—— 重复的同名 cookie 发给服务端是隐患。
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


def load_live_status_spec(account_id):
    """取 `live_status` 规格。返回 (spec, borrowed) —— borrowed=True 表示借的别人模板。

    规格是"跑过直播大屏页才学到"的,新账号/没开过播的账号没有。但实测(2026-10-28)
    这份规格里**只有三处和账号有关**:URL 的 `_aid`、body 的 `_log_finder_id`、cookie。
    拿别的账号的规格当模板、把这仨换成自己的,服务端返回的就是自己的昵称
    (4 号 -> U.S.优选仓 / 8 号 -> U.S.POLOASSN.彪壹男鞋店,各自对得上)。
    所以没规格的账号不必先去起浏览器学一轮,直接借模板就能探。
    """
    own = (_load_spec_cache(account_id) or {}).get("live_status")
    if own and own.get("url"):
        return own, False
    return _borrow_spec(account_id), True


def _borrow_spec(account_id):
    """从缓存里找任一其他账号的 `live_status` 当模板。"""
    try:
        with open(_spec_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    for aid, item in (data or {}).items():
        if aid == account_id:
            continue
        spec = (item or {}).get("live_status")
        if spec and spec.get("url"):
            return spec
    return None


def _spec_path():
    from .live_fetcher import _SPEC_CACHE_PATH
    return _SPEC_CACHE_PATH


def prepare_request(spec, account=None, borrowed=False):
    """规格 -> (url, method, headers, body)。

    - 剔除 cookie(由调用方按 storage_state 单独拼,规格落盘时已删掉)
    - 刷新 body 里的毫秒 timestamp
    - borrowed=True 时把 `_aid` / `_log_finder_id` 换成本账号的,否则会把别的账号
      标识原样发出去(那等于在问"别人在播吗",数据会张冠李戴)
    """
    url = spec.get("url") or ""
    method = (spec.get("method") or "POST").upper()
    headers = {k: v for k, v in (spec.get("headers") or {}).items()
               if k.lower() not in _SKIP_HEADERS}
    body = spec.get("postData")
    if borrowed and account:
        my_aid = account.get("_aid") or ""
        my_fid = account.get("_log_finder_id") or ""
        if my_aid:
            url = re.sub(r"_aid=[^&]+", "_aid=" + my_aid, url, count=1)
        if body and my_fid:
            body = re.sub(r'"_log_finder_id"\s*:\s*"[^"]*"',
                          '"_log_finder_id":"%s"' % my_fid, body, count=1)
    if body:
        now_ms = str(int(time.time() * 1000))
        body = re.sub(r'"timestamp"\s*:\s*"\d+"',
                      '"timestamp":"%s"' % now_ms, body, count=1)
    return url, method, headers, body


async def _post_httpx(url, headers, body, timeout):
    async with _httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
        if body is None:
            r = await cli.get(url, headers=headers)
        else:
            r = await cli.post(url, headers=headers, content=body.encode("utf-8"))
        return r.status_code, r.text


async def _post_urllib(url, headers, body, timeout):
    """标准库兜底。阻塞调用丢进线程,不占事件循环。"""
    import urllib.request
    import urllib.error

    def _call():
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        opener = urllib.request.build_opener()
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b"").decode("utf-8", "replace")

    return await asyncio.to_thread(_call)


async def _request(url, method, headers, body, timeout=DEFAULT_TIMEOUT):
    """返回 (status_code, text)。method 目前实测只用到 POST。"""
    if method == "GET":
        body = None
    if HAS_HTTPX:
        try:
            return await _post_httpx(url, headers, body, timeout)
        except Exception:
            pass          # httpx 挂了不要紧,标准库再来一次
    return await _post_urllib(url, headers, body, timeout)


def _dump_live_response(account_id, text):
    """首次探到"在播"时把完整响应留一份 —— status 在播时的真实取值一直没证据。

    只写一次(文件已存在就跳过),`data/live_probe/<账号>_live_http.json`。
    """
    try:
        path = os.path.join(_data_dir(), "live_probe", "%s_live_http.json" % account_id)
        if os.path.exists(path):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


async def probe_http(account, diag=None):
    """纯 HTTP 探测。返回判据 dict(结构同 `_probe_light`),不可用返回 None。

    结构: {ts, live, reason, status, has_stream, has_stats, via: "http"}

    diag: 可选 dict,失败时写入 `why`(no_account / no_spec / no_cookie /
    request_error / http_error / expired / badauth / parse_error)。调用方据此
    决定要不要暂停尝试 —— cookie 没了 3 分钟内不可能自己长回来,没必要每轮白等超时。
    成功时 diag 里会有 `borrowed`(是否借了别人的规格模板)。
    """
    def _no(why):
        if diag is not None:
            diag["why"] = why
        return None

    if not account:
        return _no("no_account")
    aid = account.get("id") or ""
    spec, borrowed = load_live_status_spec(aid)
    if not spec or not spec.get("url"):
        return _no("no_spec")
    cookie = build_cookie_header(account.get("storage_state"))
    if not cookie:
        return _no("no_cookie")
    url, method, headers, body = prepare_request(spec, account, borrowed)
    headers["Cookie"] = cookie
    try:
        code, txt = await _request(url, method, headers, body)
    except Exception as e:
        if diag is not None:
            diag["why"] = "request_error"
            diag["err"] = repr(e)[:120]
        return None
    if not txt or code >= 400:
        if diag is not None:
            diag["why"] = "http_error"
            diag["code"] = code
        return None
    try:
        # errCode != 0 不能当"未播"!分两种:
        #   300333 = 会话失效(实测:10-21/10-23/10-25 登录的账号全部返回这个),
        #            必须重新扫码,交回给能触发登录的路径;
        #   其他    = 未知错误,同样不算"未播",一律交回。
        head = txt.lstrip()[:200]
        if '"errCode"' in head:
            m = re.search(r'"errCode"\s*:\s*(-?\d+)', head)
            if m and int(m.group(1)) != 0:
                if diag is not None:
                    diag["why"] = "expired" if int(m.group(1)) == 300333 else "badauth"
                    diag["code"] = int(m.group(1))
                return None
    except Exception:
        pass
    if diag is not None:
        diag["borrowed"] = borrowed
    lf = None
    try:
        lf = LiveFetcher(None, aid)
        # 抑制只对常驻实例有意义的日志与首次落盘(探测每几分钟一次,不该反复写文件)
        lf.live_object_id = "-"
        lf._status_body_dumped = True
        await lf._parse_live_status(txt)
        live, reason = judge_live(
            live_stats=lf.live_stats, stats_fresh=True,
            stream_url=lf.stream_url, allow_stream_url=True,
            status=lf.status_num)
        if live:
            _dump_live_response(aid, txt)
        return {"ts": time.time(), "live": live, "reason": reason,
                "status": lf.status_num, "has_stream": bool(lf.stream_url),
                "has_stats": bool(lf.live_stats), "via": "http",
                "borrowed_spec": borrowed}
    except Exception:
        _no("parse_error")
        return None
    finally:
        lf = None
        txt = None


def backend_name():
    """诊断用:当前实际走哪个 HTTP 后端。"""
    return "httpx" if HAS_HTTPX else "urllib"
