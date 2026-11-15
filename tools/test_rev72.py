# -*- coding: utf-8 -*-
"""rev72 单测:评论写操作默认纯 HTTP + 账号隔离(多账号并行 / 单账号串行)。

本机没装 playwright,但 backend.api_client / account_manager 可真实 import;
AccountWorker / AccountManager 用 object.__new__ 造裸实例测方法本身。
所有异步用例放在同一个 asyncio.run 里(账号锁按事件循环隔离)。
"""
import ast
import asyncio
import io
import logging
import os
import sys
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backend.api_client as AC
from backend.api_client import WxApiClient, account_lock, reset_account_locks_for_test

# 本机没装 playwright -> account_manager 不整体 import,沿用 test_rev70/71
# 的套路:ast 抽出被测函数体 + fake self。
AM_SRC = io.open(os.path.join(ROOT, "backend", "account_manager.py"),
                 encoding="utf-8").read()
AM_NS = {"asyncio": asyncio, "logging": logging,
         "logger": logging.getLogger("t"), "time": time, "os": os,
         "json": __import__("json")}


def extract(name):
    tree = ast.parse(AM_SRC)
    node = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
            break
    assert node is not None, "找不到 " + name
    node.decorator_list = []
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<%s>" % name, "exec"), AM_NS)


for _n in ("_write_via_http", "hold_for_manual", "_account_fetch_lock"):
    extract(_n)


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


class LivePage:
    def is_closed(self):
        return False


def mk(acc_id="t-a", via=None, page=None, scope=None):
    cfg = {}
    rc = {}
    if via:
        rc["api_write_via"] = via
    if scope:
        rc["write_lock_scope"] = scope
    if rc:
        cfg["risk_control"] = rc
    return WxApiClient(page,
                       {"id": acc_id, "_aid": "aid-" + acc_id,
                        "_log_finder_id": "fid-" + acc_id},
                       cfg, None)


async def _noop_throttle(path):
    return None


async def _forbidden_page(path, url, body):
    raise AssertionError("纯 HTTP 写模式不应回退浏览器")


def instrument(a, key, events, delay=0.06):
    async def fake_http(path, url, body, page_url, is_write=False):
        t0 = time.perf_counter()
        events.append(("enter", key, t0))
        await asyncio.sleep(delay)
        events.append(("exit", key, time.perf_counter()))
        return {"errCode": 0, "data": {}}
    a._post_via_http = fake_http
    a._post_via_page = _forbidden_page
    a._throttle = _noop_throttle


def intervals(events):
    """返回 [(enter, exit)] 区间,按 enter 排序。"""
    out = []
    for kind, key, t in events:
        if kind == "enter":
            out.append([key, t, None])
    # 按 key 分别配对
    res = []
    for e in out:
        for kind, key, t in events:
            if kind == "exit" and key == e[0] and t >= e[1] and e[2] is None:
                e[2] = t
                break
        res.append(tuple(e))
    return sorted(res, key=lambda x: x[1])


WRITE = {"exportId": "export/test", "commentId": "c1"}


# ==================== A 段:纯 HTTP 写默认口径 ====================
def a_tests():
    a = mk()
    check("A1 默认 api_write_via=http", a._write_via == "http", a._write_via)
    check("A2 默认读也是 http", a._read_via == "http")
    b = mk(via="browser")
    check("A3 配置可回退 browser", b._write_via == "browser")
    c = mk(page=LivePage())
    check("A4 有活 page 写仍走 HTTP", c._via_http(True) is True)
    check("A5 有活 page 读仍走 HTTP", c._via_http(False) is True)
    d = mk(via="browser", page=LivePage())
    check("A6 browser 模式有活 page 走页面", d._via_http(True) is False)
    e = mk(via="browser")
    check("A7 browser 模式无页面自动降级 HTTP(rev62)", e._via_http(True) is True)
    check("A8 _lock_key 默认按 account_id", mk()._lock_key() == "t-a")
    check("A9 write_lock_scope=finder 按 finder",
          mk(scope="finder")._lock_key() == "fid-t-a")


# ==================== B 段:账号锁本身 ====================
async def b_tests():
    reset_account_locks_for_test()
    l1 = account_lock("x")
    l2 = account_lock("x")
    l3 = account_lock("y")
    check("B1 同账号同锁", l1 is l2)
    check("B2 不同账号不同锁", l1 is not l3)
    l4 = account_lock("")
    check("B3 空 key 也有锁且不与 x 共用", l4 is not l1)

    a, b = mk("t-a"), mk("t-b")
    check("B4 两个实例 account_id 不同 -> 锁不同",
          account_lock(a._lock_key()) is not account_lock(b._lock_key()))
    a2 = mk("t-a")
    check("B5 api 重建后同账号仍是同一把锁",
          account_lock(a._lock_key()) is account_lock(a2._lock_key()))


# ==================== C 段:单账号串行 / 多账号并行 ====================
async def c_tests():
    reset_account_locks_for_test()
    # C1 同账号两个并发写 -> 严格串行(第二段的 enter >= 第一段的 exit)
    ev = []
    a = mk("t-a")
    instrument(a, "a", ev)
    await asyncio.gather(a._post("comment/del_comment", dict(WRITE)),
                         a._post("comment/del_comment", dict(WRITE)))
    iv = intervals(ev)
    check("C1 同账号两次写都发出", len(iv) == 2, iv)
    check("C2 同账号严格串行(无重叠)", len(iv) == 2 and iv[1][1] >= iv[0][2], iv)

    # C3 api 重建后并发仍串行(锁表跨实例共享)
    ev2 = []
    a1, a2 = mk("t-a2"), mk("t-a2")
    instrument(a1, "old", ev2)
    instrument(a2, "new", ev2)
    await asyncio.gather(a1._post("comment/del_comment", dict(WRITE)),
                         a2._post("comment/del_comment", dict(WRITE)))
    iv2 = intervals(ev2)
    check("C3 重建实例仍串行", len(iv2) == 2 and iv2[1][1] >= iv2[0][2], iv2)

    # C4 不同账号并发 -> 区间重叠(真并行)
    ev3 = []
    x, y = mk("t-x"), mk("t-y")
    instrument(x, "x", ev3, delay=0.10)
    instrument(y, "y", ev3, delay=0.10)
    t0 = time.perf_counter()
    await asyncio.gather(x._post("comment/del_comment", dict(WRITE)),
                         y._post("comment/del_comment", dict(WRITE)))
    wall = time.perf_counter() - t0
    iv3 = intervals(ev3)
    overlap = (len(iv3) == 2 and iv3[0][1] < iv3[1][1] < iv3[0][2])
    check("C4 不同账号并发区间重叠", overlap, iv3)
    check("C5 并行墙钟 < 串行(0.20s)", wall < 0.17, "%.3fs" % wall)

    # C6 回复评论(create_comment)同样走写通道
    ev4 = []
    r = mk("t-r")
    instrument(r, "r", ev4, delay=0.01)
    resp = await r._post("comment/create_comment", {"content": "hi"})
    check("C6 回复评论走纯 HTTP 成功", isinstance(resp, dict)
          and resp.get("errCode") == 0, resp)


# ==================== D 段:零发包短路 / 不回退浏览器 ====================
async def d_tests():
    reset_account_locks_for_test()
    # D1 no_cookie(_post_via_http 返回 None) -> 不回退页面,报本地错误
    a = mk("t-nocookie")
    async def give_none(path, url, body, page_url, is_write=False):
        return None
    a._post_via_http = give_none
    a._post_via_page = _forbidden_page
    a._throttle = _noop_throttle
    r = await a._post("comment/del_comment", dict(WRITE))
    check("D1 no_cookie 返回本地错误", r == {"__err": "no_cookie"}, r)
    check("D2 no_cookie 不触发退避", a._consecutive_failures == 0)
    check("D3 no_cookie 不占每日写额度", a._daily_count(time.strftime("%Y-%m-%d")) == 0)

    # D4 block 期纯 HTTP 写 -> 零发包 + http_blocked + 豁免退避
    b = mk("t-block")
    b._http_block_until = time.time() + 999
    calls = []
    orig = AC.wx_http.post_json
    async def spy(account, url, body, page_url="", headers=None, timeout=20.0):
        calls.append(1)
        return {"errCode": 0, "data": {}}
    AC.wx_http.post_json = spy
    try:
        b._post_via_page = _forbidden_page
        b._throttle = _noop_throttle
        r2 = await b._post("comment/del_comment", dict(WRITE))
    finally:
        AC.wx_http.post_json = orig
    check("D4 block 期返回 http_blocked", r2.get("__err") == "http_blocked", r2)
    check("D5 block 期零发包", calls == [], calls)
    check("D6 block 期不触发退避", b._consecutive_failures == 0)
    check("D7 block 期不占每日写额度", b._daily_count(time.strftime("%Y-%m-%d")) == 0)

    # D8 eval_error / http_blocked 都在退避豁免集合
    check("D8 _is_failure 豁免 http_blocked",
          b._is_failure({"__err": "http_blocked"}) is False)
    check("D9 _is_failure 仍判真实业务错误",
          b._is_failure({"errCode": 300800}) is True)


# ==================== E 段:hold_for_manual 不再白开浏览器 ====================
async def e_tests():
    def bare(config, open_impl=None):
        async def _delayed(delay):
            await asyncio.sleep(999)
        async def _noop():
            return None
        w = SimpleNamespace(
            config=config, logged_in=True, account={"id": "t-a"},
            _browser_lock=asyncio.Lock(), _browser_refs=0,
            _manual_hold=False, _manual_release_task=None,
            context=None, live_fetcher=None,
            _open_context=open_impl or _noop,
            _delayed_close_manual=_delayed,
            _context_alive=lambda: False)
        w._write_via_http = lambda: AM_NS["_write_via_http"](w)
        return w

    opened = []
    async def no_open():
        opened.append(1)
        raise AssertionError("纯 HTTP 写模式不该开浏览器")
    w = bare({}, no_open)
    r = await AM_NS["hold_for_manual"](w)
    check("E1 纯 HTTP hold 直接返回 logged_in", r is True, r)
    check("E2 不开浏览器", opened == [], opened)
    check("E3 不占浏览器引用", w._browser_refs == 0)
    check("E4 不置 manual_hold", w._manual_hold is False)
    check("E5 不创建延迟回收计时器", w._manual_release_task is None)

    # E6 显式 browser 回退 -> 恢复开浏览器 + 引用计数
    opened2 = []
    async def ok_open():
        opened2.append(1)
    w2 = bare({"risk_control": {"api_write_via": "browser"}}, ok_open)
    r2 = await AM_NS["hold_for_manual"](w2)
    check("E6 browser 回退仍开浏览器", opened2 == [1], opened2)
    check("E7 browser 回退占引用", w2._browser_refs == 1)
    check("E8 browser 回退置 manual_hold", w2._manual_hold is True)
    if w2._manual_release_task is not None:
        w2._manual_release_task.cancel()

    check("E9 _write_via_http 默认 True",
          AM_NS["_write_via_http"](w) is True)
    check("E10 _write_via_http browser 时 False",
          AM_NS["_write_via_http"](w2) is False)


# ==================== F 段:抓取锁账号隔离 ====================
def f_tests():
    def manager(config):
        return SimpleNamespace(config=config, _fetch_lock=None)
    m = manager({})
    l1 = AM_NS["_account_fetch_lock"](m, "a")
    l2 = AM_NS["_account_fetch_lock"](m, "b")
    check("F1 不同账号抓取锁不同(并行)", l1 is not l2)
    m2 = manager({"risk_control": {"fetch_parallel": False}})
    l3 = AM_NS["_account_fetch_lock"](m2, "a")
    l4 = AM_NS["_account_fetch_lock"](m2, "b")
    check("F2 fetch_parallel=false 恢复共享锁", l3 is l4)
    m3 = manager({"risk_control": {"fetch_parallel": "true"}})
    check("F3 fetch_parallel=true 仍按账号隔离",
          AM_NS["_account_fetch_lock"](m3, "a") is not
          AM_NS["_account_fetch_lock"](m3, "b"))


# ==================== G 段:源码静态断言 ====================
def g_tests():
    ac = src("backend/api_client.py")
    am = src("backend/account_manager.py")
    sv = src("backend/server.py")
    check("G1 模块级账号锁表", "_ACCOUNT_LOCKS_GUARD = threading.Lock()" in ac)
    check("G2 account_lock 按事件循环隔离", "id(asyncio.get_running_loop())" in ac)
    check("G3 默认写 http", 'rc.get("api_write_via") or "http"' in ac)
    check("G4 无 browser 默认值", 'or "browser").lower()' not in ac)
    check("G5 _post 包装账号锁", "async with account_lock(self._lock_key()):" in ac)
    check("G6 _post_impl 独立", "async def _post_impl(self, path, body" in ac)
    check("G7 纯 HTTP 写不回退浏览器", 'resp = {"__err": "no_cookie"}' in ac)
    check("G8 block 零发包短路", 'self._http_give_up("blocked", path' in ac)
    check("G9 零发包不计每日额度", "零发包的本地错误" in ac)
    check("G10 退避豁免 http_blocked", '"http_blocked"' in ac)
    check("G11 hold 早退", "if self._write_via_http():" in am)
    check("G12 hold 不碰 _browser_refs", "if self._write_via_http():\n            return self.logged_in" in am)
    check("G13 两处按账号注入抓取锁", am.count("self._account_fetch_lock(") == 2)
    check("G14 fetch_parallel 回退通道", 'rc.get("fetch_parallel"' in am)
    check("G15 默认模板含 api_write_via", '"api_write_via": "http"' in sv)
    check("G16 server 默认模板含 fetch_parallel", '"fetch_parallel": True' in sv)
    # 读通道没被误改
    check("G17 读仍默认 http", 'rc.get("api_read_via") or "http"' in ac)
    check("G18 rev71 死 page 判断仍在", "def _page_alive(self):" in ac)
    check("G19 rev62 无页面降级仍在", "if not self._page_alive():" in ac)


async def _amain():
    await b_tests()
    await c_tests()
    await d_tests()
    await e_tests()


def main():
    print("=== A 纯 HTTP 写默认口径 ===")
    a_tests()
    print("=== B 账号锁 ===")
    print("=== C 串行/并行 ===")
    print("=== D 零发包短路 ===")
    print("=== E hold_for_manual ===")
    print("=== F 抓取锁账号隔离 ===")
    f_tests()
    print("=== G 源码静态断言 ===")
    g_tests()
    asyncio.run(_amain())
    print("\nTOTAL: PASS=%d FAIL=%d  %s" % (PASS, FAIL, "OK" if FAIL == 0 else "BAD"))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
