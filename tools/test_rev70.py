# -*- coding: utf-8 -*-
"""rev70 单测:①300342 降级软故障 + 指数退避 ②直播纯 HTTP 退避 1→2→4→8
③worker 路径补齐 localStorage 注入 ④登录态事件档案。

本机没有 playwright(account_manager 直接 import 会失败),沿用
test_rev69.py 的套路:live_fetcher / auth_events 真实导入,account_manager
用 ast 抽出函数体 + fake self。
"""
import ast
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


import logging
LOG = logging.getLogger("sphgj")
import backend.live_fetcher as LFM
import backend.auth_events as AE

TMP = tempfile.mkdtemp(prefix="rev70_")
AE._path = lambda: os.path.join(TMP, "auth_events.json")
AE.reset_cache_for_test()


# ============================================================
# A. LiveFetcher:300342 -> 1/2/4/8 分钟退避(真实模块)
# ============================================================
print("\nA. 直播纯 HTTP 遇 300342 退避")


def mk_lf():
    lf = LFM.LiveFetcher(None, "t-a")
    lf._learned = {"live_status": {"url": "https://x/a", "method": "POST"},
                   "live_info": {"url": "https://x/b", "method": "POST"}}
    lf.live_object_id = "oid"
    return lf


async def a_tests():
    calls = {"n": 0}

    async def fail_post(spec):
        calls["n"] += 1
        return ""

    # A1-A4 连续失败 -> 60/120/240/480
    lf = mk_lf()
    lf._http_post_spec = fail_post
    lf._http_last_why = "errCode=300342"
    waits = []
    for _ in range(4):
        lf._http_backoff_until = 0.0
        t0 = time.time()
        await lf._poll_http()
        waits.append(int(round(lf._http_backoff_until - t0)))
    check("A1 退避序列 1→2→4→8 分钟", waits == [60, 120, 240, 480], waits)
    check("A2 kick_streak=4", lf._http_kick_streak == 4, lf._http_kick_streak)

    # A5 封顶 480
    last = 0
    for _ in range(4):
        lf._http_backoff_until = 0.0
        t0 = time.time()
        await lf._poll_http()
        last = int(round(lf._http_backoff_until - t0))
    check("A5 退避封顶 8 分钟", last == 480, last)

    # A6 退避期内一个包都不发
    before = calls["n"]
    snap = await lf._poll_http()
    check("A6 退避期内不发请求", calls["n"] == before, "%d->%d" % (before, calls["n"]))
    check("A7 退避期仍返回快照(不关流)", isinstance(snap, dict) and "stream_url" in snap)

    # A8 成功后清零
    async def ok_post(spec):
        return json.dumps({"errCode": 0, "data": {}})

    lf2 = mk_lf()
    lf2._http_post_spec = ok_post
    lf2._http_last_why = "errCode=300342"
    lf2._http_kick_streak = 5
    await lf2._poll_http()
    check("A8 成功后 kick_streak 清零", lf2._http_kick_streak == 0, lf2._http_kick_streak)
    check("A9 成功后退避清零", lf2._http_backoff_until == 0.0, lf2._http_backoff_until)

    # A10 非 300342 不设退避(仍是普通连续失败计数)
    lf3 = mk_lf()
    lf3._http_post_spec = fail_post
    lf3._http_last_why = "errCode=300333"
    await lf3._poll_http()
    check("A10 300333 不进退避", lf3._http_backoff_until == 0.0, lf3._http_backoff_until)
    check("A11 300333 计入普通失败", lf3._poll_fail_streak == 1, lf3._poll_fail_streak)


# ============================================================
# B. account_manager._probe_via_http:300342 -> 软故障
# ============================================================
print("\nB. HTTP 探测被顶 = 软故障(不判离线)")


class FakeProbe:
    """替身 live_probe_http:按脚本返回 diag。"""
    def __init__(self):
        self.queue = []
        self.calls = 0

    async def probe_http(self, acc, diag):
        self.calls += 1
        if not self.queue:
            return {"ts": time.time(), "live": False, "reason": "x", "via": "http"}
        why, code = self.queue.pop(0)
        diag["why"] = why
        if code:
            diag["code"] = code
        return None


class FakeSelf:
    def __init__(self, probe):
        self.account = {"id": "t-b"}
        self.config = {"accounts": [{"id": "t-b"}]}
        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""
        self._soft_fail_streak = 0
        self._soft_block_until = 0.0
        self.probe = probe


async def b_tests():
    probe = FakeProbe()
    ns = {"time": time, "logger": LOG, "live_probe_http": probe, "auth_events": AE}
    fn = extract("backend/account_manager.py", "_probe_via_http", ns)
    me = FakeSelf(probe)

    # B1 首次被顶 -> 60s,streak=1,且绝不判离线(fake self 没有 mark_offline)
    probe.queue = [("kicked", 300342)]
    r = await fn(me)
    check("B1 被顶返回 None(交回浏览器路径)", r is None)
    check("B2 被顶 streak=1", me._soft_fail_streak == 1, me._soft_fail_streak)
    wait1 = int(round(me._soft_block_until - time.time()))
    check("B3 首次退避 60s", 58 <= wait1 <= 60, wait1)
    check("B4 被顶不清 _http_block_until", me._http_block_until == 0.0)

    # B5 退避期内不再发请求
    n0 = probe.calls
    await fn(me)
    check("B5 软故障退避期内零请求", probe.calls == n0, "%d->%d" % (n0, probe.calls))

    # B6 退避过期后第二次 -> 120s
    me._soft_block_until = 0.0
    probe.queue = [("kicked", 300342)]
    await fn(me)
    wait2 = int(round(me._soft_block_until - time.time()))
    check("B6 第二次退避 120s", 118 <= wait2 <= 120, wait2)
    check("B7 第二次 streak=2", me._soft_fail_streak == 2, me._soft_fail_streak)

    # B8 封顶 900
    me._soft_fail_streak = 20
    me._soft_block_until = 0.0
    probe.queue = [("kicked", 300342)]
    await fn(me)
    waitn = int(round(me._soft_block_until - time.time()))
    check("B8 退避封顶 900s", 898 <= waitn <= 900, waitn)

    # B9 恢复 -> 清零
    me._soft_block_until = 0.0
    probe.queue = []
    r = await fn(me)
    check("B9 恢复后返回判据", isinstance(r, dict) and r.get("via") == "http")
    check("B10 恢复后 streak 清零", me._soft_fail_streak == 0, me._soft_fail_streak)
    check("B11 恢复后退避清零", me._soft_block_until == 0.0, me._soft_block_until)

    # B12 300333 仍是硬故障:180s 固定,不动软故障计数
    me2 = FakeSelf(probe)
    probe.queue = [("expired", 300333)]
    await fn(me2)
    wait3 = int(round(me2._http_block_until - time.time()))
    check("B12 300333 固定 180s", 178 <= wait3 <= 180, wait3)
    check("B13 300333 不改软故障计数", me2._soft_fail_streak == 0, me2._soft_fail_streak)

    # B14 事件档案里留了 http_kicked
    kinds = [e["kind"] for e in AE.recent("t-b", 100)]
    check("B14 事件档案记录 http_kicked", "http_kicked" in kinds, kinds)
    check("B15 事件档案记录 soft_recover", "soft_recover" in kinds, kinds)
    check("B16 事件档案记录 http_expired", "http_expired" in kinds, kinds)
    kicked = [e for e in AE.recent("t-b", 100) if e["kind"] == "http_kicked"]
    check("B17 kicked 事件带 300342", kicked and kicked[0].get("code") == 300342,
          kicked[:1])


# ============================================================
# C. 只注 cookie 不算登录 -> 必须同时回填 localStorage
# ============================================================
print("\nC. worker 路径 cookie + localStorage 双注入")


class FakeCtx:
    def __init__(self):
        self.cookies = []
        self.scripts = []

    async def add_cookies(self, ck):
        self.cookies.extend(ck)

    async def add_init_script(self, js):
        self.scripts.append(js)


async def c_tests():
    state = {
        "cookies": [{"name": "sessionid", "value": "S", "domain": ".weixin.qq.com",
                     "path": "/", "expires": -1, "httpOnly": True, "secure": True,
                     "sameSite": "None"}],
        "origins": [{"origin": "https://channels.weixin.qq.com",
                     "localStorage": [{"name": "__ml::aid", "value": "AID1"},
                                      {"name": "finder_username", "value": "finder1"}]}],
    }
    made = {}

    async def fake_launch(pw, profile, headless=True):
        ctx = FakeCtx()
        made["ctx"] = ctx
        return ctx

    ns = {"launch_stealth": fake_launch, "inject_login_state":
          __import__("backend.local_window", fromlist=["x"]).inject_login_state,
          "logger": LOG}
    fake_self = type("S", (), {})()
    fake_self._pw = None
    fake_self._headless = True
    fake_self.account = {"id": "t-c", "profile_dir": "./profiles/_t_c",
                         "storage_state": state}
    fn = extract("backend/account_manager.py", "_open_context_light", ns)
    fn.__globals__["launch_stealth"] = fake_launch
    ctx = await fn(fake_self)
    check("C1 轻量探测注入 cookie", len(ctx.cookies) == 1, len(ctx.cookies))
    check("C2 轻量探测注入 localStorage 脚本", len(ctx.scripts) == 1, len(ctx.scripts))
    js = ctx.scripts[0] if ctx.scripts else ""
    check("C3 脚本含 __ml::aid", "__ml::aid" in js)
    check("C4 脚本含 finder_username", "finder_username" in js)
    check("C5 脚本含 channels origin", "channels.weixin.qq.com" in js)

    # C6 只有 cookies 没有 origins:不应崩,也不注册空脚本
    made.clear()
    fake_self.account = {"id": "t-c2", "profile_dir": "./profiles/_t_c2",
                         "storage_state": {"cookies": state["cookies"]}}
    ctx2 = await fn(fake_self)
    check("C6 无 origins 时只注 cookie", len(ctx2.cookies) == 1 and not ctx2.scripts,
          (len(ctx2.cookies), len(ctx2.scripts)))

    # C7 完全没有 storage_state:不崩
    fake_self.account = {"id": "t-c3", "profile_dir": "./profiles/_t_c3"}
    ctx3 = await fn(fake_self)
    check("C7 无登录态时不崩", len(ctx3.cookies) == 0 and not ctx3.scripts)

    # C8 worker 主路径(_open_context)也必须走 inject_login_state
    am = src("backend/account_manager.py")
    check("C8 _open_context 用 inject_login_state",
          'inject_login_state(self.context, _ss, self.account["id"], "worker")' in am)
    import re
    nude = re.findall(r"await\s+(?:self\.)?context\.add_cookies\(", am)
    check("C9 account_manager 无裸 add_cookies 调用", not nude, nude)
    dy = src("backend/douyin/worker.py")
    check("C10 抖音 worker 也走 inject_login_state",
          "inject_login_state(self.context, ss, self.account_id" in dy)


# ============================================================
# D. 登录态事件档案
# ============================================================
print("\nD. 事件档案 data/auth_events.json")


def d_tests():
    # D 段用独立档案文件(B 段已经往共享文件里写过事件)
    global TMP
    AE._path = lambda: os.path.join(TMP, "auth_events_d.json")
    AE.reset_cache_for_test()
    p = AE._path()
    if os.path.exists(p):
        os.remove(p)
    AE.reset_cache_for_test()
    ok = AE.record("t-d", "offline", code=300333, reason="自然过期")
    check("D1 record 返回 True", ok)
    check("D2 文件已落盘", os.path.exists(p))
    with io.open(p, encoding="utf-8") as f:
        data = json.load(f)
    check("D3 结构含 version/events", data.get("version") == 1 and len(data["events"]) == 1)
    ev = data["events"][0]
    check("D4 事件字段齐全", ev["account_id"] == "t-d" and ev["code"] == 300333
          and ev["kind"] == "offline" and "ts" in ev and "t" in ev)

    AE.record("t-d", "online", reason="恢复")
    AE.record("t-e", "offline", code=300342)
    check("D5 recent 倒序", [x["kind"] for x in AE.recent("t-d", 10)] == ["online", "offline"])
    check("D6 recent 按账号过滤", len(AE.recent("t-e", 10)) == 1)
    check("D7 recent 全量", len(AE.recent(None, 10)) == 3)
    check("D8 recent limit 生效", len(AE.recent(None, 2)) == 2)
    sm = AE.summary(7)
    check("D9 summary 按账号统计", sm.get("t-d", {}).get("offline") == 1
          and sm.get("t-e", {}).get("offline") == 1, sm)

    # D10 超量砍头
    AE.reset_cache_for_test()
    for i in range(AE.MAX_EVENTS + 25):
        AE.record("t-f", "offline", code=i)
    with io.open(p, encoding="utf-8") as f:
        data = json.load(f)
    check("D10 上限截断", len(data["events"]) == AE.MAX_EVENTS, len(data["events"]))
    check("D11 砍掉最老的", data["events"][0]["code"] == 25, data["events"][0]["code"])

    # D12 坏文件容错
    with io.open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    AE.reset_cache_for_test()
    check("D12 坏文件不抛异常", AE.record("t-g", "offline") is True)
    check("D13 坏文件后 re recent 可用", len(AE.recent("t-g", 5)) == 1)

    # D14 record 永不抛(模拟写盘失败)
    old_save = AE._save
    AE._save = lambda d: (_ for _ in ()).throw(IOError("boom"))
    try:
        check("D14 写盘失败不抛", AE.record("t-h", "offline") is False)
    finally:
        AE._save = old_save


# ============================================================
# E. 源码级铁律
# ============================================================
print("\nE. 铁律")


def e_tests():
    am = src("backend/account_manager.py")
    lf = src("backend/live_fetcher.py")
    ph = src("backend/live_probe_http.py")
    check("E1 300342 归为 kicked", '"kicked" if _c == 300342 else "badauth"' in ph)
    check("E1b 300333 仍归 expired", '"expired" if _c == 300333' in ph)
    check("E2 探测入口双重退避", "_now < self._http_block_until or _now < self._soft_block_until" in am)
    check("E3 被顶不判离线(代码里无 mark_offline 调用)", "mark_offline(" not in
          am.split('if why == "kicked":')[1].split("if why in (")[0])
    check("E4 直播退避不关流(不调 close)", "await self.close()" not in
          lf.split('if "300342" in why:')[1].split("elif n in (3, 10)")[0])
    check("E5 mark_offline 落档案", 'auth_events.record(self.account["id"], "offline"' in am)
    check("E6 mark_online 落档案", 'auth_events.record(self.account["id"], "online"' in am)


async def main():
    await a_tests()
    await b_tests()
    await c_tests()
    d_tests()
    e_tests()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n==== PASS=%d FAIL=%d ====" % (PASS, FAIL))
    sys.exit(0 if FAIL == 0 else 1)
