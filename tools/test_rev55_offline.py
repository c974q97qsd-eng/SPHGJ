# -*- coding: utf-8 -*-
"""rev55 单测:把「掉线状态准确 -> 根本不发包」补齐三处缺口。

用户 2026-10-28 的要求:
    「现在每轮会先试 HTTP，只要你掉线状态是准确的，掉线状态的账号就不用去发包了，
      直到扫码登录后才加入轮询任务。」

rev54 实现了「运行中掉线 -> mark_offline -> _fetch_loop 不发包」,
rev55 补的三处:
  1. 启动时就未登录 -> 要落盘(否则每次点启动都白开一次 Chromium,且 auth_state 永远 unknown)
  2. 已有「硬验未登录」明确结论 + context 还开着 -> 不必再发一个包去撞 300333
  3. 硬验结论必须随 context 一起作废(否则拿过期证据判离线)

跑法:python tools/test_rev55_offline.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _StubMod(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.endswith("Error"):
            v = type(name, (Exception,), {})
        elif name[0].isupper():
            v = type(name, (), {})
        else:
            def v(*a, **k):
                return None
        setattr(self, name, v)
        return v


for _name in ("playwright", "playwright.async_api", "playwright._impl",
              "playwright._impl._errors"):
    sys.modules.setdefault(_name, _StubMod(_name))
sys.modules["playwright"].async_api = sys.modules["playwright.async_api"]

from backend import account_manager as am
from backend.account_manager import AccountWorker, AccountManager, auth_fingerprint

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name + (" | " + str(extra) if extra else ""))
        print(f"  FAIL  {name}  {extra}")


COOKIE_ACC = {
    "id": "t1",
    "profile_dir": os.path.join(tempfile.gettempdir(), "rev55_profile"),
    "_aid": "123",
    "_log_finder_id": "finderX",
    "storage_state": {"cookies": [
        {"name": "sessionid", "value": "AAA", "domain": ".weixin.qq.com", "path": "/"},
        {"name": "wxuin", "value": "999", "domain": ".weixin.qq.com", "path": "/"},
    ]},
}


def make_worker(acc=None):
    ev = []

    async def _emit(event, payload):
        ev.append((event, payload))

    async def _noop(*a, **k):
        return None

    w = AccountWorker(dict(acc or COOKIE_ACC), None, {}, _emit)
    w._events = ev
    w.on_auth_change = _noop
    return w


class FakeFetcher:
    def __init__(self, res=(1, 2, [], [], True)):
        self.calls = 0
        self.res = res

    async def fetch_all(self, on_batch=None):
        self.calls += 1
        return self.res


# ==================== 1. 缺口2:已有明确结论就不再发包 ====================
async def t_no_packet_when_verified_offline():
    w = make_worker()
    f = FakeFetcher()
    w.fetcher = f
    w.context = object()          # context 还开着
    w._verify_result = "no"       # 且刚硬验过:没有登录身份
    w.logged_in = False
    res, used = await w._run_fetch_round()
    check("已硬验未登录:", w.auth_state == "offline", w.auth_state)
    check("已硬验未登录:一轮请求都不发", f.calls == 0, f.calls)
    check("已硬验未登录:返回值是空轮", res is None, res)
    check("已硬验未登录:不占浏览器引用", used is False, used)
    check("已硬验未登录:引用计数归零", w._browser_refs == 0, w._browser_refs)

    # 再跑一轮:因为已经是 offline,_fetch_loop 会直接等事件,这里直接验证早退
    res2, used2 = await w._run_fetch_round()
    check("离线后再跑一轮仍然不发包", f.calls == 0 and res2 is None, f.calls)


# ==================== 2. 缺口3:硬验结论随 context 作废 ====================
async def t_verify_result_reset():
    w = make_worker()
    w._verify_result = "no"
    w.context = object()
    try:
        await w._close_context()
    except Exception:
        pass
    check("关 context 后硬验结论作废", w._verify_result is None, w._verify_result)

    # 残留证据不该单独判离线:context 已关,只有 _verify_result 残留(此处人为塞回)
    w2 = make_worker()
    w2.fetcher = FakeFetcher()
    w2._verify_result = "no"
    w2.context = None
    w2.logged_in = False
    try:
        await w2._run_fetch_round()
    except Exception:
        pass
    check("context 已关时不拿残留证据判离线", w2.auth_state != "offline", w2.auth_state)


# ==================== 3. 缺口1:启动未登录要落盘 + 第二次不再开浏览器 ====================
class FakeWorker:
    instances = []

    def __init__(self, acc, storage, config, emit):
        self.account = acc
        self.auth_state = "unknown"
        self.auth_reason = ""
        self.logged_in = False        # 模拟:硬验未登录
        self._running = False
        self._browser_refs = 0
        self._opened = False
        self.stopped = False
        self.on_auth_change = None
        self._fetch_lock = None
        self._cycle_offset = 0.0
        FakeWorker.instances.append(self)

    async def start(self, pw, headless=True):
        self._opened = True

    async def stop(self):
        self.stopped = True
        self._running = False


class FakeStorage:
    def account_stats(self):
        return []


async def t_start_account_offline():
    tmp = tempfile.mkdtemp(prefix="rev55_")
    acc = dict(COOKIE_ACC)
    m = AccountManager({"accounts": [acc], "fetch_interval_sec": 600}, FakeStorage(), None)

    async def _emit(e, p):
        return None

    async def _ensure_pw():
        return None

    m.emit = _emit
    m._ensure_playwright = _ensure_pw
    m._auth_file = os.path.join(tmp, "auth_state.json")
    m._auth_offline = {}
    m._run_intent_file = os.path.join(tmp, "run_state.json")
    m._run_intent = set()

    queued = []

    async def _enq(aid):
        queued.append(aid)

    m.enqueue_auto_relogin = _enq

    real_worker = am.AccountWorker
    am.AccountWorker = FakeWorker
    try:
        w1 = await m.start_account("t1")
        check("启动未登录:返回 logged_in=False", getattr(w1, "logged_in", None) is False)
        check("启动未登录:不入 workers", "t1" not in m.workers, list(m.workers))
        check("启动未登录:开过一次浏览器", FakeWorker.instances[0]._opened is True)
        check("启动未登录:context 已停", FakeWorker.instances[0].stopped is True)
        check("启动未登录:落盘 auth_state.json", os.path.exists(m._auth_file), m._auth_file)
        if os.path.exists(m._auth_file):
            d = json.loads(io.open(m._auth_file, encoding="utf-8").read())
            check("启动未登录:记录里有该账号", "t1" in d, d)
            check("启动未登录:记录了原因", bool((d.get("t1") or {}).get("reason")), d)
            check("启动未登录:指纹等于当前登录态",
                  (d.get("t1") or {}).get("fp") == auth_fingerprint(acc), d)
        check("启动未登录:不重复排队扫码(前端自己弹)", queued == [], queued)

        n_before = len(FakeWorker.instances)
        w2 = await m.start_account("t1")
        check("二次启动:不再开浏览器", len(FakeWorker.instances) == n_before,
              len(FakeWorker.instances))
        check("二次启动:返回 None(跳过启动)", w2 is None, w2)
        check("二次启动:排队扫码", queued == ["t1"], queued)
    finally:
        am.AccountWorker = real_worker


# ==================== 4. 缺口3:快照对非 worker 账号说真话 ====================
async def t_snapshot_view():
    tmp = tempfile.mkdtemp(prefix="rev55b_")
    acc = dict(COOKIE_ACC)
    m = AccountManager({"accounts": [acc]}, FakeStorage(), None)
    m._auth_file = os.path.join(tmp, "auth_state.json")
    m._auth_offline = {}

    snap = m.status_snapshot()
    a = snap["accounts"][0]
    check("快照:没记录时是 unknown", a["auth_state"] == "unknown", a["auth_state"])

    m._auth_offline = {"t1": {"reason": "启动时 auth_data 硬验未通过(未登录)",
                              "ts": 1.0, "fp": auth_fingerprint(acc)}}
    a2 = m.status_snapshot()["accounts"][0]
    check("快照:已落盘掉线就报 offline", a2["auth_state"] == "offline", a2["auth_state"])
    check("快照:带上原因", "硬验" in (a2["auth_reason"] or ""), a2["auth_reason"])

    class W:
        auth_state = "ok"
        auth_reason = ""
        auth_ts = None
        logged_in = True
        _running = True

    m.workers["t1"] = W()
    a3 = m.status_snapshot()["accounts"][0]
    check("快照:worker 在线时以 worker 为准", a3["auth_state"] == "ok", a3["auth_state"])


# ==================== 5. 回归:offline 账号不进轮询 ====================
async def t_fetch_loop_guard():
    w = make_worker()
    f = FakeFetcher()
    w.fetcher = f
    w.context = object()
    w._verify_result = "no"
    w.logged_in = False
    await w.mark_offline("测试")
    check("mark_offline 后 auth_state=offline", w.auth_state == "offline", w.auth_state)
    check("mark_offline 后 event 已清", w._online_event.is_set() is False)
    check("mark_offline 后引用计数 0", w._browser_refs == 0, w._browser_refs)
    res, used = await w._run_fetch_round()
    check("offline 账号跑抓取轮:0 请求", f.calls == 0 and res is None, f.calls)
    await w.mark_online("扫码成功")
    check("mark_online 恢复 ok", w.auth_state == "ok", w.auth_state)
    check("mark_online 置位 event", w._online_event.is_set() is True)


async def main():
    print("== 1. 已有明确结论就不再发包")
    await t_no_packet_when_verified_offline()
    print("== 2. 硬验结论随 context 作废")
    await t_verify_result_reset()
    print("== 3. 启动未登录要落盘 + 第二次跳过")
    await t_start_account_offline()
    print("== 4. 快照对非 worker 账号说真话")
    await t_snapshot_view()
    print("== 5. 回归:offline 不进轮询")
    await t_fetch_loop_guard()
    print(f"\n合计 {PASS + FAIL} 项:PASS {PASS} / FAIL {FAIL}")
    if FAILS:
        print("失败项:")
        for x in FAILS:
            print("  - " + x)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
