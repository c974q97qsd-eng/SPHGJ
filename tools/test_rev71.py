# -*- coding: utf-8 -*-
"""rev71 单测:浏览器 context / page「对象还在但底层已死」的存活判断与回退。

本机没装 playwright -> api_client 可真实 import(只依赖 wx_http),
account_manager 沿用 test_rev70 的套路:ast 抽出函数体 + fake self。
"""
import ast
import asyncio
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.api_client import WxApiClient
import backend.api_client as AC

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, extra))


def src(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def extract(rel, name, ns):
    tree = ast.parse(src(rel))
    node = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
            break
    assert node is not None, "找不到 %s" % name
    node.decorator_list = []
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<%s>" % name, "exec"), ns)
    return ns[name]


class FakePage:
    def __init__(self, closed=False, boom=False):
        self.closed = closed
        self.boom = boom
        self.evaluated = 0

    def is_closed(self):
        if self.boom:
            raise RuntimeError("context disposed")
        return self.closed

    async def evaluate(self, *a, **k):
        self.evaluated += 1
        raise RuntimeError("Page.evaluate: Target page, context or browser has been closed")


class FakeCtx:
    def __init__(self, pages):
        self._pages = pages

    @property
    def pages(self):
        return self._pages


class FakeTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return True

    def cancel(self):
        self.cancelled = True


class FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAsyncio:
    CancelledError = asyncio.CancelledError

    @staticmethod
    def create_task(coro):
        # 立刻关掉协程,别留 pending task
        try:
            coro.close()
        except Exception:
            pass
        return FakeTask()


def mk_api(page=None):
    """绕过 __init__ 造一个最小 WxApiClient。"""
    o = WxApiClient.__new__(WxApiClient)
    o.page = page
    o._read_via = "http"
    o._write_via = "browser"
    o._write_http_accounts = set()
    o._http_block_until = 0.0
    o._http_last_why = ""
    o._http_logged = False
    o.auth_fail = None
    o.account_id = "t-account"
    o.account = {"id": "t-account"}
    o.config = {"accounts": [o.account]}
    o.aid = "t-aid"
    o.finder_id = "fid"
    o._consecutive_failures = 0
    o._backoff_until = 0.0
    o._daily_write_limit = 100
    o._storage = None
    o.calls = []
    return o


print("\nA. api_client:死 page 等价「无页面」")

# A1 _page_alive
a = mk_api()
check("A1 page=None -> False", a._page_alive() is False)
a.page = FakePage(closed=False)
check("A2 活 page -> True", a._page_alive() is True)
a.page = FakePage(closed=True)
check("A3 死 page -> False", a._page_alive() is False)
a.page = FakePage(boom=True)
check("A4 is_closed 抛异常 -> False", a._page_alive() is False)

# A5 _via_http 写操作
a = mk_api(page=FakePage(closed=True))
check("A5 死 page 写操作走 HTTP", a._via_http(True) is True)
a.page = FakePage(closed=False)
check("A6 活 page 写操作默认走浏览器", a._via_http(True) is False)
a.page = None
check("A7 无 page 写操作走 HTTP(rev62 不回归)", a._via_http(True) is True)

# A8 _post_via_page 死 page 不 evaluate
a = mk_api(page=FakePage(closed=True))
r = asyncio.run(a._post_via_page("comment/del_comment", "http://x", {}))
check("A8 死 page 直接返回 no_page", r == {"__err": "no_page"}, r)
check("A9 死 page 未调用 evaluate", a.page.evaluated == 0, a.page.evaluated)

# A10 _is_failure 本地通道错误豁免
a = mk_api()
for e in ("eval_error", "eval_timeout", "no_cookie", "no_page:foo", "no_page"):
    check("A10 %s 不触发退避" % e, a._is_failure({"__err": e}) is False)
for r0 in ({"ret": 1}, {"errCode": 300333}, {"__err": "parse"}, {"base_resp": {"ret": 2}}, {}):
    ok = a._is_failure(r0)
    check("A11 真实失败仍判失败 %s" % str(r0)[:32], ok is (r0 != {}), ok)
check("A12 成功响应不判失败", a._is_failure({"errCode": 0, "data": {}}) is False)

# A13 failed() 仍判失败(上层不会误报成功)
check("A13 failed(eval_error)=True", WxApiClient.failed({"__err": "eval_error"}) is True)
check("A14 failed(成功)=False", WxApiClient.failed({"errCode": 0}) is False)

# A15 端到端 _post:死 page -> 走 HTTP 成功
a = mk_api(page=FakePage(closed=True))
a.calls = []


async def _throttle(p):
    a.calls.append("throttle")


async def _post_via_http(path, url, body, page_url, is_write=False):
    a.calls.append("http")
    return {"errCode": 0, "data": {"ok": 1}}


a._throttle = _throttle
a._post_via_http = _post_via_http
a._daily_count = lambda d: 0
a._latest_account = lambda: a.account
a._on_failure = lambda: a.calls.append("FAIL")
a._on_success = lambda: a.calls.append("OK")
a._incr_daily_count = lambda d: None
resp = asyncio.run(a._post("comment/del_comment", {"exportId": "export/abc", "commentId": "c1"}))
check("A15 死 page -> HTTP 成功", resp == {"errCode": 0, "data": {"ok": 1}}, resp)
check("A16 HTTP 被调用", "http" in a.calls, a.calls)
check("A17 未走 evaluate", a.page.evaluated == 0, a.page.evaluated)
check("A18 计成功不计失败", "OK" in a.calls and "FAIL" not in a.calls, a.calls)
check("A19 失败计数保持 0", a._consecutive_failures == 0, a._consecutive_failures)

# A20 端到端 _post:死 page 且 HTTP 被挡 -> no_page,不 evaluate
b = mk_api(page=FakePage(closed=True))
b.calls = []
b._http_block_until = 9999999999.0
b._http_last_why = "blocked"
b._throttle = lambda p: asyncio.sleep(0)
b._daily_count = lambda d: 0
b._latest_account = lambda: b.account
b._on_failure = lambda: b.calls.append("FAIL")
b._on_success = lambda: b.calls.append("OK")
b._incr_daily_count = lambda d: None
resp2 = asyncio.run(b._post("comment/del_comment", {"exportId": "export/abc"}))
check("A20 HTTP 被挡时返回 no_page:blocked", resp2.get("__err") == "no_page:blocked", resp2)
check("A21 未走 evaluate", b.page.evaluated == 0)
check("A22 本地通道错误不计失败", "FAIL" not in b.calls and b._consecutive_failures == 0, b.calls)

# A23 活 page 仍走页面(不回归默认行为)
c = mk_api(page=FakePage(closed=False))
c.calls = []
c._throttle = lambda p: asyncio.sleep(0)
c._daily_count = lambda d: 0
c._latest_account = lambda: c.account
c._on_failure = lambda: None
c._on_success = lambda: None
c._incr_daily_count = lambda d: None


async def _via_page(path, url, body):
    c.calls.append("page")
    return {"errCode": 0}


c._post_via_page = _via_page
resp3 = asyncio.run(c._post("comment/del_comment", {"exportId": "e"}))
check("A23 活 page 仍走页面 fetch", "page" in c.calls and resp3 == {"errCode": 0}, c.calls)


print("\nB. account_manager:死 context 自愈")

import logging
AM = "backend/account_manager.py"
ns = {
    "logger": logging.getLogger("sphgj"),
    "WxApiClient": WxApiClient,
    "asyncio": FakeAsyncio,
    "time": __import__("time"),
    "random": __import__("random"),
}
ctx_alive = extract(AM, "_context_alive", dict(ns))
drop_dead = extract(AM, "_drop_dead_context", dict(ns))
ensure_browser = extract(AM, "ensure_browser", dict(ns))
hold_manual = extract(AM, "hold_for_manual", dict(ns))


class FakeWorker:
    def __init__(self, ctx=None, alive=True):
        self.context = ctx
        self.page = FakePage(closed=False)
        self.live_page = object()
        self.live_fetcher = None
        self._verify_result = "ok"
        self.account = {"id": "t-account"}
        # rev72: 本段测的是 browser 回退路径(纯 HTTP 写模式已不碰浏览器,
        # 早退行为由 test_rev72 的 E 段覆盖),这里显式声明,别让桩误走早退。
        self.config = {"accounts": [self.account],
                       "risk_control": {"api_write_via": "browser"}}
        self._write_via_http = lambda: False
        self.storage = None
        self.logged_in = True
        self._browser_refs = 0
        self._browser_lock = FakeLock()
        self._manual_hold = False
        self._manual_release_task = None
        self._opened = 0
        self.api = None
        self.auto_reply = self.auto_commenter = self.auto_delete = self.fetcher = None
        self._context_alive = lambda: ctx_alive(self)
        self._drop_dead_context = lambda: drop_dead(self)

    async def _open_context(self):
        self._opened += 1
        self.context = FakeCtx([FakePage(closed=False)])
        return None

    async def _delayed_close_manual(self, delay):
        return None


# B1 _context_alive 五种状态
w = FakeWorker(ctx=None)
check("B1 context=None -> False", ctx_alive(w) is False)
w.context = FakeCtx([])
check("B2 pages 为空 -> False", ctx_alive(w) is False)
w.context = FakeCtx([FakePage(closed=True)])
check("B3 所有 page 已关 -> False", ctx_alive(w) is False)
w.context = FakeCtx([FakePage(closed=True), FakePage(closed=False)])
check("B4 有一个活 page -> True", ctx_alive(w) is True)
w.context = object()
check("B5 取 pages 抛异常 -> False", ctx_alive(w) is False)


# B6 _drop_dead_context:重建 api + 继承状态 + 重绑组件
class LF:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


w = FakeWorker(ctx=FakeCtx([FakePage(closed=True)]))
old = mk_api(page=FakePage(closed=True))
old.auth_fail = "300333"
old._http_block_until = 12345.0
old._http_logged = True
w.api = old
for attr in ("auto_reply", "auto_commenter", "auto_delete", "fetcher"):
    setattr(w, attr, type("C", (), {"api": old})())
lf = LF()
w.live_fetcher = lf
asyncio.run(drop_dead(w))
check("B6 context/page/live_page/live_fetcher 全清", all(
    getattr(w, k) is None for k in ("context", "page", "live_page", "live_fetcher")))
check("B7 _verify_result 清空", w._verify_result is None)
check("B8 api 重建且 page=None", w.api is not old and w.api.page is None)
check("B9 继承 auth_fail", w.api.auth_fail == "300333")
check("B10 继承 _http_block_until", w.api._http_block_until == 12345.0)
check("B11 继承 _http_logged", w.api._http_logged is True)
check("B12 四个组件重绑到新 api", all(
    getattr(w, k).api is w.api for k in ("auto_reply", "auto_commenter", "auto_delete", "fetcher")))
check("B13 live_fetcher.close 被调用", lf.closed is True)

# B14 ensure_browser 死 context 时重建
w = FakeWorker(ctx=FakeCtx([FakePage(closed=True)]))
r = asyncio.run(ensure_browser(w))
check("B14 死 context -> 重建", w._opened == 1, w._opened)
check("B15 重建后返回 logged_in", r is True)
w2 = FakeWorker(ctx=FakeCtx([FakePage(closed=False)]))
asyncio.run(ensure_browser(w2))
check("B16 活 context 不重复开", w2._opened == 0, w2._opened)

# B17 hold_for_manual 死 context 时重建
w = FakeWorker(ctx=FakeCtx([FakePage(closed=True)]))
r = asyncio.run(hold_manual(w, delay=120))
check("B17 死 context -> 重建", w._opened == 1, w._opened)
check("B18 手动持有置位", w._manual_hold is True)
check("B19 引用计数 +1", w._browser_refs == 1, w._browser_refs)
check("B20 返回 logged_in", r is True)
w3 = FakeWorker(ctx=FakeCtx([FakePage(closed=False)]))
asyncio.run(hold_manual(w3, delay=120))
check("B21 活 context 不重复开", w3._opened == 0, w3._opened)

# B22 _open_context 幂等判断
ns2 = dict(ns)
open_ctx = extract(AM, "_open_context", ns2)
w = FakeWorker(ctx=FakeCtx([FakePage(closed=True)]))
w.api = mk_api(page=FakePage(closed=True))
w._open_context = lambda: asyncio.sleep(0)   # 占位,真实开头会先 drop
# 只验证开头逻辑:活 context 应直接 return(不触发任何调用)
w4 = FakeWorker(ctx=FakeCtx([FakePage(closed=False)]))
asyncio.run(open_ctx(w4))
check("B22 活 context 直接幂等返回", w4.context is not None and w4._opened == 0)


print("\nC. 源码防回归断言")
am_s, ac_s = src(AM), src("backend/api_client.py")
check("C1 三处开浏览器入口都用活体判断", am_s.count("if not self._context_alive():") == 2
      and "if self._context_alive():" in am_s)
_seg = am_s.split("async def _drop_dead_context")[1].split("async def _open_context")[0]
check("C2 _drop_dead_context 不调 close_context_safely", "close_context_safely(" not in _seg)
check("C3 _close_context 原有重建逻辑仍在(rev54c 不回归)", "if self.api is not None:" in
      am_s.split("async def _close_context")[1].split("async def ensure_browser")[0])
check("C4 _page_alive 三处调用", ac_s.count("not self._page_alive()") == 3,
      ac_s.count("not self._page_alive()"))
check("C4b _open_context 死 context 会 drop", "await self._drop_dead_context()" in am_s)
check("C5 eval_error/eval_timeout 已豁免", '"eval_error", "eval_timeout"' in ac_s)
check("C6 no_page 前缀豁免仍在(rev62 不回归)", 'str(err).startswith("no_page")' in ac_s)
check("C7 failed() 未改", 'if resp.get("__err"):\n            return True' in ac_s)
check("C8 两个源码可编译", True)
compile(am_s, AM, "exec")
compile(ac_s, "backend/api_client.py", "exec")

print("\nRESULT: %d PASS / %d FAIL" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
