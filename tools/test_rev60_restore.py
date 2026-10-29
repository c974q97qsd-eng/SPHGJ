# -*- coding: utf-8 -*-
"""rev60 单测:启动被停机打断时,不许把账号误判成「未登录」并踢出恢复列表。

复现的现场(母本 run.log 01:19:46,同一秒):
    [shutdown] 开始清理(thread):账号进程 0 个        <- 恢复还在进行中,workers 是空的
    [shutdown] 清理完成,用时 0.06s
    ERROR  [acc2] 启动开浏览器失败: Page.wait_for_timeout: Target page…closed
    WARNING [恢复] acc2 未登录或启动失败,已移出恢复列表
结果:data/auth_state.json 把好账号记成 offline + 运行意图被扔掉。

跑法:python tools/test_rev60_restore.py
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------- 本地没装 playwright,stub 掉只为能 import 链路 ----------
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

from backend import account_manager as AM            # noqa: E402
from backend.account_manager import AccountWorker, AccountManager   # noqa: E402

PASS = FAIL = 0
FAILS = []


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name + (" | " + str(extra) if extra else ""))
        print(f"  FAIL  {name}  {extra}")


TMP = tempfile.mkdtemp(prefix="rev60_")


def acc_of(aid, cookies=0):
    ck_list = []
    for i in range(cookies):
        ck_list.append({"name": "sessionid%d" % i, "value": "V%d" % i,
                        "domain": ".weixin.qq.com", "path": "/"})
    return {"id": aid, "name": aid, "profile_dir": os.path.join(TMP, "prof_" + aid),
            "_aid": "123", "_log_finder_id": aid + "_fid",
            "storage_state": {"cookies": ck_list}}


class FakeStorage:
    """status_snapshot / worker.start 会摸 storage,给个空实现就够了。"""

    def account_stats(self):
        return []

    def load_window_states(self, *_a, **_k):
        return {}


def new_manager(accounts, intent=()):
    """真 AccountManager,但落盘文件指向临时目录。"""
    cfg = {"accounts": [dict(a) for a in accounts], "fetch_interval_sec": 600,
           "auto_reply": {}, "auto_delete": {}}
    m = AccountManager(cfg, FakeStorage(), None)
    m._run_intent_file = os.path.join(TMP, "run_state.json")
    m._auth_file = os.path.join(TMP, "auth_state.json")
    for f in (m._run_intent_file, m._auth_file):
        if os.path.exists(f):
            os.remove(f)
    m._run_intent = set(intent)
    m._auth_offline = {}

    async def _noop_emit(event, payload):
        return None

    async def _noop_aw(*a, **k):
        return None

    m._emit = _noop_emit
    m._ensure_playwright = _noop_aw          # 别真去拉 playwright 驱动
    return m


def read_json(p):
    if not os.path.exists(p):
        return None
    return json.load(io.open(p, encoding="utf-8"))


def running_on_disk(m):
    """run_state.json 的形状是 {"running": [...], "saved_at": ...}。"""
    d = read_json(m._run_intent_file) or {}
    return set(d.get("running") or [])


# ==================================================================
# FakeWorker:把 start_account 的分支完全握在手里
# ==================================================================
class FakeWorker:
    """按 target 决定 start() 之后的状态。"""

    made = []          # (account_id, target, 被 stop 过)

    def __init__(self, acc, storage, config, emit):
        self.acc = acc
        self.id = acc["id"]
        self.logged_in = False
        self._verify_result = None
        self._start_error = None
        self.live_fetcher = None
        self.stopped = False
        self.started = False
        # status_snapshot 会读这几个
        self._running = True
        self.last_scan_at = None
        self.new_count = 0
        self.auth_state = "unknown"
        self.auth_reason = ""
        FakeWorker.made.append(self)

    async def start(self, playwright=None, headless=True, **_kw):
        self.started = True
        t = getattr(self, "target", "ok")
        if t == "ok":
            self.logged_in = True
            self._verify_result = "ok"
        elif t == "no":
            self.logged_in = False
            self._verify_result = "no"
        elif t == "broken":
            # 浏览器被砍:logged_in 仍是 False,_verify_result 停在 None
            self.logged_in = False
            self._verify_result = None
            self._start_error = ("RuntimeError: Page.wait_for_timeout: "
                                 "Target page, context or browser has been closed")
        else:
            raise RuntimeError("start 直接抛异常")

    def start_loop(self):
        pass

    def start_live_loop(self):
        pass

    def start_live_probe(self):
        pass

    async def stop(self):
        self.stopped = True


_ORIG_WORKER = AM.AccountWorker


def use_fake_worker(target_by_aid):
    """替换 manager 里的 AccountWorker;target_by_aid: aid -> ok/no/broken/raise"""
    def factory(acc, storage, config, emit):
        w = FakeWorker(acc, storage, config, emit)
        w.target = target_by_aid.get(acc["id"], "ok")
        return w
    AM.AccountWorker = factory


def restore_worker():
    AM.AccountWorker = _ORIG_WORKER


# ==================================================================
# 1. 真 AccountWorker.start():_start_error 记录
# ==================================================================
def t_real_worker_start_error():
    print("\n[1] 真 AccountWorker.start():_open_context 抛异常时必须留证据")
    orig = AccountWorker._open_context

    probe = acc_of("probe1")
    os.makedirs(probe["profile_dir"], exist_ok=True)

    async def boom(self):
        raise RuntimeError("Page.wait_for_timeout: Target page, context or browser "
                           "has been closed\n  extra line")

    AccountWorker._open_context = boom
    w = AccountWorker(dict(probe), None, {}, None)
    try:
        asyncio.run(w.start(None, headless=True))
    except Exception as e:                     # start 本身不该抛
        ck("start() 不把异常抛给调用方", False, e)
        return
    finally:
        AccountWorker._open_context = orig

    ck("start() 不把异常抛给调用方", True)
    ck("logged_in 仍是 False", w.logged_in is False)
    ck("★ _verify_result 停在 None(= 没验成,不是未登录)", w._verify_result is None,
       w._verify_result)
    _se = getattr(w, "_start_error", "<字段不存在>")
    ck("★ _start_error 记下了原因", bool(_se) and _se != "<字段不存在>", _se)
    ck("_start_error 只取首行(不把整段 browser log 灌进去)",
       "\n" not in (_se if isinstance(_se, str) else "x"), _se)
    ck("_start_error 带异常类型名",
       isinstance(_se, str) and _se.startswith("RuntimeError:"), _se)

    # 成功路径要清空,不能残留上一次的失败原因
    async def ok_open(self):
        self.context = object()
        self.logged_in = True
        self._verify_result = "ok"

    AccountWorker._open_context = ok_open
    w2 = AccountWorker(dict(probe), None, {}, None)
    try:
        w2._start_error = "上一轮的旧错误"
    except Exception:
        pass
    try:
        asyncio.run(w2.start(None, headless=True))
    finally:
        AccountWorker._open_context = orig
    ck("成功路径把 _start_error 清空(不残留旧错误)",
       getattr(w2, "_start_error", "<字段不存在>") is None,
       getattr(w2, "_start_error", "<字段不存在>"))
    ck("成功路径 logged_in=True", w2.logged_in is True)


# ==================================================================
# 2. start_account 的三条分支
# ==================================================================
def t_start_account_branches():
    print("\n[2] start_account:区分「没验成」与「确认未登录」")

    # 2a. 没验成 -> 不记账、保留 intent
    a = acc_of("a1", cookies=1)
    m = new_manager([a], intent={"a1"})
    use_fake_worker({"a1": "broken"})
    try:
        w = asyncio.run(m.start_account("a1"))
    finally:
        restore_worker()
    disk = read_json(m._auth_file)
    ck("a) 没验成:不写 auth_state.json", disk in (None, {}), disk)
    ck("a) 没验成:不进 workers", "a1" not in m.workers)
    ck("a) 没验成:运行意图保留", "a1" in m._run_intent, m._run_intent)
    ck("a) 没验成:返回 worker(带 _start_error 供上层判断)",
       isinstance(w, FakeWorker) and bool(w._start_error))
    ck("a) 没验成:把没起来的 worker 收拾掉(不残留浏览器)", w.stopped is True)

    # 2b. 确认未登录 -> 照旧记账(rev55 行为不能回退)
    a2 = acc_of("a2", cookies=1)
    m2 = new_manager([a2], intent={"a2"})
    use_fake_worker({"a2": "no"})
    try:
        w2 = asyncio.run(m2.start_account("a2"))
    finally:
        restore_worker()
    disk2 = read_json(m2._auth_file) or {}
    ck("b) 确认未登录:落盘 auth_state", "a2" in disk2, disk2)
    ck("b) 确认未登录:reason 是硬验未通过",
       "硬验未通过" in (disk2.get("a2") or {}).get("reason", ""), disk2.get("a2"))
    ck("b) 确认未登录:带上登录态指纹(供下次指纹比对)",
       bool((disk2.get("a2") or {}).get("fp")))
    ck("b) 确认未登录:不进 workers", "a2" not in m2.workers)
    ck("b) 确认未登录:worker 被关掉", w2.stopped is True)

    # 2c. 登录成功 -> 进 workers + 记 intent
    a3 = acc_of("a3", cookies=1)
    m3 = new_manager([a3])
    use_fake_worker({"a3": "ok"})
    try:
        w3 = asyncio.run(m3.start_account("a3"))
    finally:
        restore_worker()
    ck("c) 登录成功:进 workers", "a3" in m3.workers)
    ck("c) 登录成功:运行意图已记录", "a3" in m3._run_intent)
    ck("c) 登录成功:落盘 run_state(下次重启能恢复)",
       "a3" in running_on_disk(m3), running_on_disk(m3))
    ck("c) 登录成功:不写离线记录", "a3" not in (read_json(m3._auth_file) or {}))
    ck("c) 登录成功:没被误关", w3.stopped is False)


# ==================================================================
# 3. 停机标志的守卫
# ==================================================================
def t_shutdown_guards():
    print("\n[3] 停机标志:_shutting_down 期间一律收手")

    a = acc_of("g1", cookies=1)
    m = new_manager([a], intent={"g1"})
    m._shutting_down = True
    before = len(FakeWorker.made)
    use_fake_worker({"g1": "ok"})
    try:
        w = asyncio.run(m.start_account("g1"))
    finally:
        restore_worker()
    ck("a) 停机中 start_account 早退返回 None", w is None)
    ck("a) 停机中压根不构造 worker(不浪费一次 Chromium)", len(FakeWorker.made) == before)
    ck("a) 停机中不动运行意图", "g1" in m._run_intent, m._run_intent)

    # _remember_offline 停机拒绝落盘
    m2 = new_manager([a], intent={"g1"})
    m2._shutting_down = True
    asyncio.run(m2._remember_offline("g1", a, "启动时 auth_data 硬验未通过(未登录)"))
    ck("b) 停机中 _remember_offline 不落盘", read_json(m2._auth_file) in (None, {}),
       read_json(m2._auth_file))
    ck("b) 停机中内存里也不写离线记录", "g1" not in m2._auth_offline, m2._auth_offline)

    # 非停机时照旧落盘(回归)
    m3 = new_manager([a], intent={"g1"})
    asyncio.run(m3._remember_offline("g1", a, "xx"))
    ck("c) 非停机时 _remember_offline 正常落盘(回归)",
       "g1" in (read_json(m3._auth_file) or {}))

    # start() 复位标志
    m4 = new_manager([a])
    m4._shutting_down = True

    async def _noop(*a, **k):
        return None
    m4._ensure_playwright = _noop
    m4._memtrim = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    # 只验证标志复位这一句(不跑完整引擎启动)
    m4._shutting_down = False
    ck("d) 引擎 start() 会复位 _shutting_down(源码级确认)",
       "self._shutting_down = False\n        self._running = True" in
       io.open(os.path.join(ROOT, "backend", "account_manager.py"), encoding="utf-8").read())


# ==================================================================
# 4. restore_running_accounts
# ==================================================================
def t_restore():
    print("\n[4] restore_running_accounts:停机收手 + 只认确定性未登录")

    # 4a. 停机中进入 -> 立刻收手,一个 intent 都不动
    accs = [acc_of("r1", cookies=1), acc_of("r2", cookies=1)]
    m = new_manager(accs, intent={"r1", "r2"})
    m._shutting_down = True
    use_fake_worker({})
    try:
        asyncio.run(m.restore_running_accounts(gap_sec=0.05))
    finally:
        restore_worker()
    ck("a) 停机中:一个都不启动", not m.workers, list(m.workers))
    ck("a) 停机中:运行意图全部保留", m._run_intent == {"r1", "r2"}, m._run_intent)
    ck("a) 停机中:不写 auth_state", read_json(m._auth_file) in (None, {}))

    # 4b. 第一个没验成 -> 保留 intent,第二个照常继续
    m2 = new_manager([acc_of("r1", cookies=1), acc_of("r2", cookies=1)],
                     intent={"r1", "r2"})
    use_fake_worker({"r1": "broken", "r2": "ok"})
    try:
        asyncio.run(m2.restore_running_accounts(gap_sec=0.02))
    finally:
        restore_worker()
    ck("b) 没验成:运行意图保留(★ 以前会被 discard)", "r1" in m2._run_intent, m2._run_intent)
    ck("b) 没验成:不写 auth_state(不把好号记成掉线)",
       "r1" not in (read_json(m2._auth_file) or {}))
    ck("b) 没验成:不影响后面的账号继续恢复", "r2" in m2.workers)

    # 4c. 确认未登录 -> discard + 落盘
    m3 = new_manager([acc_of("r1", cookies=1)], intent={"r1"})
    use_fake_worker({"r1": "no"})
    try:
        asyncio.run(m3.restore_running_accounts(gap_sec=0.02))
    finally:
        restore_worker()
    ck("c) 确认未登录:移出运行意图", "r1" not in m3._run_intent, m3._run_intent)
    ck("c) 确认未登录:落盘 auth_state", "r1" in (read_json(m3._auth_file) or {}))
    ck("c) 确认未登录:run_state 同步落盘(下次重启不再试)",
       "r1" not in running_on_disk(m3), running_on_disk(m3))

    # 4d. 启动过程中开始停机 -> 保留 intent
    m4 = new_manager([acc_of("r1", cookies=1), acc_of("r2", cookies=1)],
                     intent={"r1", "r2"})

    class Stopper(FakeWorker):
        async def start(self, playwright=None, headless=True, **_kw):
            self.started = True
            try:
                m4._shutting_down = True    # 模拟:启动这一秒收到重启指令
            except Exception:
                pass
            self.logged_in = False
            self._verify_result = None
            self._start_error = "RuntimeError: Target page, context or browser has been closed"

    def factory(acc, storage, config, emit):
        return Stopper(acc, storage, config, emit)
    AM.AccountWorker = factory
    try:
        asyncio.run(m4.restore_running_accounts(gap_sec=0.02))
    finally:
        restore_worker()
    ck("d) 启动中撞停机:运行意图保留(★ 现场就是这里丢的)",
       m4._run_intent == {"r1", "r2"}, m4._run_intent)
    ck("d) 启动中撞停机:不写 auth_state", read_json(m4._auth_file) in (None, {}))
    ck("d) 启动中撞停机:后续账号不再启动", "r2" not in m4.workers)


# ==================================================================
# 5. stop_all 收拾「启动中」的 worker
# ==================================================================
def t_stop_all():
    print("\n[5] stop_all:把「正在启动中」的 worker 也一起关掉")

    accs = [acc_of("s1", cookies=1)]
    m = new_manager(accs, intent={"s1"})
    w = FakeWorker(dict(accs[0]), None, {}, None)
    _st = getattr(m, "_starting", None)
    if _st is None:
        ck("manager 有 _starting 登记表(记录启动到一半的 worker)", False, "字段不存在")
        _st = {}
    else:
        ck("manager 有 _starting 登记表(记录启动到一半的 worker)", True)
        _st["s1"] = w                          # 模拟:start 进行到一半,还没进 workers
    ck("前置:workers 为空(现场就是 0 个)", not m.workers)

    m._memtrim = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    m._playwright = None
    asyncio.run(m.stop_all())

    ck("★ _starting 里的 worker 被关掉(以前完全没人管)", w.stopped is True)
    ck("_starting 被清空", not getattr(m, "_starting", {}), getattr(m, "_starting", {}))
    ck("★ stop_all 置 _shutting_down=True",
       getattr(m, "_shutting_down", False) is True,
       getattr(m, "_shutting_down", "<字段不存在>"))
    ck("运行意图不被 stop_all 清掉(用户点停止才清)", "s1" in m._run_intent, m._run_intent)


# ==================================================================
# 6. 端到端复现母本现场
# ==================================================================
def t_replay_incident():
    print("\n[6] 端到端复现母本 01:19:46(恢复途中收到重启指令)")

    accs = [acc_of("x1", cookies=1), acc_of("x2", cookies=1), acc_of("x3", cookies=1)]
    m = new_manager(accs, intent={"x1", "x2", "x3"})
    m._memtrim = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
    m._playwright = None

    seen = {}

    class HangingWorker(FakeWorker):
        """x1 启动时卡在 _open_context,这期间 stop_all 把浏览器砍掉。"""
        async def start(self, playwright=None, headless=True, **_kw):
            self.started = True
            seen["reached"] = True
            # 模拟 wait_for_timeout:让出控制权,等待被外部打断
            for _ in range(200):
                if getattr(m, "_shutting_down", False):
                    break
                await asyncio.sleep(0.01)
            if getattr(m, "_shutting_down", False):
                self.logged_in = False
                self._verify_result = None
                self._start_error = ("RuntimeError: Page.wait_for_timeout: Target page, "
                                     "context or browser has been closed")
            else:
                self.logged_in = True
                self._verify_result = "ok"

    def factory(acc, storage, config, emit):
        return HangingWorker(acc, storage, config, emit)
    AM.AccountWorker = factory

    async def scenario():
        task = asyncio.create_task(m.restore_running_accounts(gap_sec=0.02))
        # 等 x1 真的进到启动流程里
        for _ in range(200):
            if seen.get("reached"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        stopped_at = []
        for k, v in list(m._starting.items()):
            stopped_at.append(k)
        # 此刻正是母本那一刻:workers 空、_starting 有 1 个
        snapshot = {"workers": list(m.workers), "starting": stopped_at,
                    "intent": set(m._run_intent)}
        await m.stop_all()                     # == 收到重启指令
        await asyncio.wait_for(task, timeout=10)
        return snapshot, asyncio.create_task(asyncio.sleep(0))

    try:
        snap = asyncio.get_event_loop().run_until_complete(scenario()) if False else \
            asyncio.run(scenario())
    finally:
        restore_worker()

    snapshot = snap[0] if isinstance(snap, tuple) else snap
    ck("现场复现:停机那一刻 workers 为空(与母本日志一致)", snapshot["workers"] == [],
       snapshot["workers"])
    ck("现场复现:停机那一刻有账号正在启动中", len(snapshot["starting"]) == 1,
       snapshot["starting"])
    ck("现场复现:停机前意图是 3 个", snapshot["intent"] == {"x1", "x2", "x3"},
       snapshot["intent"])
    ck("★★ 修好后:3 个运行意图全部保留(现场会只剩 2 个)", 
       m._run_intent == {"x1", "x2", "x3"}, m._run_intent)
    ck("★★ 修好后:auth_state.json 没有被污染(现场会把 x1 记成 offline)",
       read_json(m._auth_file) in (None, {}), read_json(m._auth_file))
    ck("修好后:_starting 已清空", not getattr(m, "_starting", {}),
       getattr(m, "_starting", "<字段不存在>"))


# ==================================================================
def main():
    t_real_worker_start_error()
    t_start_account_branches()
    t_shutdown_guards()
    t_restore()
    t_stop_all()
    t_replay_incident()

    print("\n" + "=" * 66)
    print(f"PASS {PASS}   FAIL {FAIL}")
    if FAILS:
        print("失败项:")
        for x in FAILS:
            print("  -", x)
    print("=" * 66)
    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
