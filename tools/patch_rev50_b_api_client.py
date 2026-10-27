# -*- coding: utf-8 -*-
"""rev50 步骤2: api_client 读路径改 HTTP 直连（P0 核心 —— 6 个调用方一次受益）。

路由策略:
  读 (post_list / comment_list / update_feed_comment)  -> wx_http 直连
  写 (create_comment / del_comment / set_top_comment / update_visible / ...) -> 浏览器
  任何 HTTP 侧异常(errCode!=0 / no_cookie / 网络错) 一律**自动回退浏览器**

开关: config.risk_control.api_read_via / api_write_via = "http" | "browser"
"""
import io
import sys

P = r"D:\SPHGJ\backend\api_client.py"

s = io.open(P, encoding="utf-8").read()
orig = len(s)

# ---------- 1) 文件头 ----------
OLD_HEAD = '''"""视频号助手评论 API 封装。

通过 Playwright page.evaluate(fetch) 调用,自动带登录 cookie(同源)。'''
NEW_HEAD = '''"""视频号助手评论 API 封装。

rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py),写接口默认仍走浏览器。
      两条路返回结构完全一致,调用方无需感知。HTTP 任何异常自动回退浏览器。
      2026-10-28 实测:纯 HTTP 与页面内 fetch 同源同 cookie,只是少了跨进程 IPC。
      原说明如下 —— 通过 Playwright page.evaluate(fetch) 调用,自动带登录 cookie(同源)。'''
assert s.count(OLD_HEAD) == 1, "文件头未命中"
s = s.replace(OLD_HEAD, NEW_HEAD, 1)

# ---------- 2) import ----------
OLD_IMP = '''import json
import time
import random
import asyncio
import logging
import uuid
from urllib.parse import quote

logger = logging.getLogger("sphgj")'''
NEW_IMP = '''import json
import re
import time
import random
import asyncio
import logging
import uuid
from urllib.parse import quote

from . import wx_http

logger = logging.getLogger("sphgj")'''
assert s.count(OLD_IMP) == 1, "import 段未命中"
s = s.replace(OLD_IMP, NEW_IMP, 1)

# ---------- 3) __init__ 加开关 ----------
OLD_INIT = '''        self._storage = storage
        self._last_req_time = 0.0
        rc = ((config or {}).get("risk_control")) or {}'''
NEW_INIT = '''        self._storage = storage
        self._last_req_time = 0.0
        self.config = config or {}
        rc = ((config or {}).get("risk_control")) or {}
        # rev50: 读/写分流。读走 HTTP 直连,写默认仍走浏览器 —— 写操作的风控表现
        # 没有任何实测证据(TLS 指纹与真浏览器不同),先留退路,要切再改配置。
        self._read_via = str(rc.get("api_read_via") or "http").lower()
        self._write_via = str(rc.get("api_write_via") or "browser").lower()
        self._http_block_until = 0.0     # no_cookie 时短期禁用,别每轮白跑
        self._http_last_why = ""'''
assert s.count(OLD_INIT) == 1, "__init__ 未命中"
s = s.replace(OLD_INIT, NEW_INIT, 1)

# ---------- 4) _post 重写 + 新增 _via_http/_latest/_post_via_http/_post_via_page ----------
OLD_POST = '''    async def _post(self, path, body, base=None, page_url=None):
        # 写操作每日上限:超限跳过,不调 API(不计退避)
        is_write = self._is_write(path)
        today = time.strftime("%Y-%m-%d") if is_write else None
        if is_write and self._daily_count(today) >= self._daily_write_limit:
            logger.warning(f"[api] 写操作达每日上限 {self._daily_write_limit},跳过 {path}")
            return {"__err": "daily_write_limit", "limit": self._daily_write_limit}
        await self._throttle(path)
        base = base or BASE
        page_url = page_url or PAGE_URL
        url = f"{base}/{path}?_aid={self.aid}&_pageUrl={quote(page_url)}"
        js = """
        async (args) => {
            try {
                const r = await fetch(args.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(args.body),
                    credentials: 'include'
                });
                return await r.text();
            } catch(e) { return JSON.stringify({__err: e.message}); }
        }
        """
        try:
            result = await asyncio.wait_for(self.page.evaluate(js, {"url": url, "body": body}), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[api] evaluate 超时(30s) path={path}")
            return {"__err": "eval_timeout"}
        try:
            resp = json.loads(result)
        except Exception:
            resp = {"__err": "parse", "raw": (result or "")[:200]}
        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
            self._on_failure()
        else:
            self._on_success()
            if is_write:
                self._incr_daily_count(today)
        return resp'''

NEW_POST = '''    async def _post(self, path, body, base=None, page_url=None):
        # 写操作每日上限:超限跳过,不调 API(不计退避)
        is_write = self._is_write(path)
        today = time.strftime("%Y-%m-%d") if is_write else None
        if is_write and self._daily_count(today) >= self._daily_write_limit:
            logger.warning(f"[api] 写操作达每日上限 {self._daily_write_limit},跳过 {path}")
            return {"__err": "daily_write_limit", "limit": self._daily_write_limit}
        await self._throttle(path)
        base = base or BASE
        page_url = page_url or PAGE_URL
        url = f"{base}/{path}?_aid={self.aid}&_pageUrl={quote(page_url)}"
        # rev50: 读走 HTTP 直连(快、不占 renderer),写默认仍走浏览器。
        # 任何 HTTP 侧异常一律回退浏览器 —— 页面里是"活会话",永远比 storage_state 新。
        resp = None
        if self._via_http(is_write):
            resp = await self._post_via_http(path, url, body, page_url)
        if resp is None:
            resp = await self._post_via_page(path, url, body)
        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
            self._on_failure()
        else:
            self._on_success()
            if is_write:
                self._incr_daily_count(today)
        return resp

    # ==================== rev50: HTTP 直连 ====================

    def _via_http(self, is_write):
        """本次走 HTTP 还是浏览器。读默认 http,写默认 browser。"""
        if time.time() < self._http_block_until:
            return False
        want = self._write_via if is_write else self._read_via
        return want == "http"

    def _latest_account(self):
        """每次现取 config 里的账号 —— 重新扫码后 storage_state / _aid 会被换成新的,
        握着的旧引用会拿到过期 cookie(rev37 踩过:worker 以为登录着,HTTP 侧是空的)。"""
        aid = self.account_id
        if aid:
            for a in (self.config.get("accounts") or []):
                if a.get("id") == aid:
                    return a
        return self.account

    @staticmethod
    def _err_code(resp):
        """取 errCode(ret 也认)。取不到返回 None。"""
        if not isinstance(resp, dict):
            return None
        for key in ("errCode", "ret"):
            v = resp.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        base = resp.get("base_resp")
        if isinstance(base, dict):
            v = base.get("ret")
            if isinstance(v, int):
                return v
        return None

    def _http_give_up(self, why, path, long_block=False):
        """HTTP 不可用 -> 记录原因并让调用方回退浏览器。"""
        if long_block:
            # cookie 不会在 3 分钟内自己长回来,别每轮白跑一次 HTTP
            self._http_block_until = time.time() + 180
        if self._http_last_why != why:
            self._http_last_why = why
            logger.info(f"[api] HTTP 直连不可用({why}),回退浏览器 path={path}"
                        + ("(180s 内不再尝试)" if long_block else ""))
        return None

    async def _post_via_http(self, path, url, body, page_url):
        """进程内 HTTP 直连。返回 dict;None = 走不通,请回退浏览器。

        只认「成功」。任何非 0 errCode 都不当作业务失败 —— 直接退浏览器重发一次,
        因为浏览器里是活的 sessionid,能给出真正可信的答案。
        """
        acc = self._latest_account()
        # _aid 每次登录都会变;URL 是早先拼的,这里按最新值纠正
        aid = (acc.get("_aid") or "").strip()
        if aid and aid != self.aid:
            url = re.sub(r"_aid=[^&]+", "_aid=" + aid, url, count=1)
        try:
            resp = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=30)
        except Exception as e:
            return self._http_give_up("exception:" + repr(e)[:60], path)
        err = resp.get("__err") if isinstance(resp, dict) else None
        if err == "no_cookie":
            return self._http_give_up("no_cookie", path, long_block=True)
        if err:
            return self._http_give_up(err, path)
        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。都不算"业务失败",交回浏览器
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333))
        return resp

    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
        js = """
        async (args) => {
            try {
                const r = await fetch(args.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(args.body),
                    credentials: 'include'
                });
                return await r.text();
            } catch(e) { return JSON.stringify({__err: e.message}); }
        }
        """
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(js, {"url": url, "body": body}), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[api] evaluate 超时(30s) path={path}")
            return {"__err": "eval_timeout"}
        except Exception as e:
            logger.warning(f"[api] evaluate 异常 path={path}: {e}")
            return {"__err": "eval_error"}
        try:
            return json.loads(result)
        except Exception:
            return {"__err": "parse", "raw": (result or "")[:200]}'''

assert s.count(OLD_POST) == 1, "_post 未唯一命中"
s = s.replace(OLD_POST, NEW_POST, 1)

# ---------- 5) fetch_wx_name 也走 HTTP ----------
OLD_WX = '''    async def fetch_wx_name(self):
        """调 auth/auth_data(POST 空 body)取登录微信昵称(data.userAttr.nickname)。"""
        await self._throttle("auth/auth_data")
        url = f"{AUTH_BASE}/auth/auth_data?_aid={self.aid}&_pageUrl={quote(PAGE_URL)}"'''
NEW_WX = '''    async def fetch_wx_name(self):
        """调 auth/auth_data(POST 空 body)取登录微信昵称(data.userAttr.nickname)。

        rev50: 先试 HTTP 直连(这条读路径同样不需要浏览器),失败回退页面 fetch。
        """
        await self._throttle("auth/auth_data")
        acc = self._latest_account()
        aid = (acc.get("_aid") or self.aid or "").strip()
        url = f"{AUTH_BASE}/auth/auth_data?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
        try:
            j = await wx_http.post_json(acc, url, {}, page_url=PAGE_URL, timeout=20)
            if not j.get("__err") and self._err_code(j) in (0, None):
                nick = ((j.get("data") or {}).get("userAttr") or {}).get("nickname") or ""
                if nick:
                    return nick
        except Exception:
            pass'''
assert s.count(OLD_WX) == 1, "fetch_wx_name 未命中"
s = s.replace(OLD_WX, NEW_WX, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------- 自校验 ----------
chk = io.open(P, encoding="utf-8").read()
must = [
    "from . import wx_http",
    "async def _post_via_http(",
    "async def _post_via_page(",
    "def _via_http(",
    "def _latest_account(",
    "def _err_code(",
    "api_read_via",
    "api_write_via",
]
must_not = ["await asyncio.wait_for(self.page.evaluate(js, {\"url\": url, \"body\": body}), timeout=30)"]
bad = [m for m in must if m not in chk]
if bad:
    print("SELFCHECK FAIL missing:", bad)
    sys.exit(1)
print("OK  %d -> %d 字节 (+%d)" % (orig, len(chk), len(chk) - orig))
print("SELFCHECK PASS")
