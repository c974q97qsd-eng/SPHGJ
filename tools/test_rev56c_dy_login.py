# -*- coding: utf-8 -*-
"""rev56j/k 离线单测：抖音在网状态机 + 登录会话的落盘逻辑。

本机没装 playwright（`import playwright` 必失败），所以这里只测**不需要浏览器**的部分：
  · 在网状态机：已知离线零启动、确定性未登录才 need_scan、code 8 才判离线
  · 状态持久化：落盘/读取/指纹失效（重新扫码后离线记录作废）
  · 登录会话：profile 目录命名、finalize 写出的账号字段、_close 不自杀

这些正是「误判把好账号踢出轮询」「扫完码白扫一次」这两类事故的防线，
比界面细节重要得多，所以必须有离线回归。
"""
import asyncio
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.douyin import api                      # noqa: E402
from backend.douyin import worker as W              # noqa: E402
from backend.douyin.login import DyLoginSession, _safe_name   # noqa: E402

PASS, FAIL = 0, []


def ck(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  " + str(extra)) if extra else ""))
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


def acc(aid="dy_aaa", sec="SEC1", uid="100", pdir=None):
    return {"id": aid, "platform": "dy", "name": "测试号",
            "_dy_sec_uid": sec, "_dy_uid": uid,
            "profile_dir": pdir or "./profiles/%s" % aid}


class FakeManager:
    """只实现 worker 用到的那几个方法。"""

    def __init__(self, account, verify=None, fetch=None):
        self._account = account
        self._verify = verify or (lambda: {"ok": True, "reason": "", "identity": {}})
        self._fetch = fetch or (lambda: {"__ok": True, "works": 1, "checked": 1, "new": 0,
                                         "skipped": 0, "complete": True})
        self.page = "somepage"
        self.set_page_calls = []

    @property
    def account_id(self):
        return (self._account or {}).get("id") or ""

    def account(self):
        return self._account or {}

    def set_page(self, page):
        self.set_page_calls.append(page)
        self.page = page

    async def verify_login(self):
        r = self._verify()
        return r() if callable(r) else r

    async def refresh_identity(self):
        return {}

    async def fetch_now(self, max_works=None):
        return self._fetch()


class FakeStorage:
    pass


def mkworker(mgr, accd=None):
    """注意：worker 的账号和 manager 的账号必须是**同一个 dict**，
    否则测的是两个不同的账号（第一版测试就栽在这——参数传错位，
    导致「重新扫码作废离线记录」那条一直假失败）。"""
    a = accd if isinstance(accd, dict) else (mgr.account() if mgr else acc())
    w = W.DouyinWorker(a, {}, FakeStorage(), mgr, emit=None)
    return w


def mk(sec="SEC1", uid="100", aid="dy_aaa"):
    """按 sec_uid 造一个 worker（worker 与 manager 共用同一账号 dict）。"""
    a = acc(sec=sec, uid=uid, aid=aid)
    return mkworker(FakeManager(a), a)


# ---------------- 用例 ----------------
def t_fp():
    print("\n[1] 登录态指纹：用平台稳定身份，不用 cookie")
    a = acc(sec="S1", uid="1")
    b = acc(sec="S1", uid="1")
    c = acc(sec="S2", uid="1")
    d = acc(sec="", uid="")
    ck(W._auth_fp(a) == W._auth_fp(b), "同身份 -> 同指纹")
    ck(W._auth_fp(a) != W._auth_fp(c), "重新扫码(sec_uid 变了) -> 指纹变")
    ck(W._auth_fp(d) != W._auth_fp(a), "没身份 -> 与有身份不同")
    ck(len(W._auth_fp(a)) == 32, "指纹是 md5")


async def t_state_offline():
    print("\n[2] 已知离线 -> 一个浏览器都不开（零发包）")
    w = mkworker(FakeManager(acc()))
    w._remember_offline("上次掉线")
    called = {"open": 0}

    async def fake_open():
        called["open"] += 1
        return True
    w._open = fake_open
    r = await w.start(playwright=object())
    ck(r.get("ok") is False and r.get("offline") is True, "返回 offline")
    ck(r.get("need_scan") is True, "提示需要扫码")
    ck(called["open"] == 0, "完全没有开浏览器", called["open"])
    ck(w.auth_state == "offline", "状态置为 offline")

    # force 覆盖
    w2 = mkworker(FakeManager(acc()))
    w2._remember_offline("上次掉线")
    hit = {"n": 0}

    async def ok_open():
        hit["n"] += 1
        w2.auth_state = "ok"
        return True
    w2._open = ok_open
    r2 = await w2.start(playwright=object(), force=True)
    ck(hit["n"] == 1 and r2.get("ok") is True, "force=True 时照常启动")


async def t_need_scan():
    print("\n[3] need_scan 只给确定性未登录（浏览器起不来时不要叫用户扫码）")
    # 用独立账号：t_state_offline 已经把 dy_aaa/SEC1 记成离线了，
    # 复用同一个账号会走进「已知离线」的早退分支，测不到我们要测的东西。
    w = mk("SEC_T3", aid="dy_t3")

    async def fail_unverified():
        w.auth_state = "unknown"
        w.auth_reason = "登录态未验成（eval_error）"
        return False
    w._open = fail_unverified
    r = await w.start(playwright=object())
    ck(r.get("need_scan") is False, "没验成 -> need_scan=False", r.get("need_scan"))
    ck(r.get("offline") is False, "没验成 -> 不判离线")

    w2 = mk("SEC_T3B", aid="dy_t3b")

    async def fail_offline():
        w2.auth_state = "offline"
        w2.auth_reason = "抖音未登录（status_code=8）"
        return False
    w2._open = fail_offline
    r2 = await w2.start(playwright=object())
    ck(r2.get("need_scan") is True, "确定未登录 -> need_scan=True")
    ck(r2.get("offline") is True, "确定未登录 -> offline=True")


async def t_multi_account():
    print("\n[4] 多账号保护：非首个 dy 账号拒绝启动（否则数据会记错归属）")
    mgr = FakeManager(acc(aid="dy_first"))
    w = mkworker(mgr, acc(aid="dy_second"))
    r = await w.start(playwright=object())
    ck(r.get("ok") is False, "拒绝启动")
    ck("只支持单个抖音账号" in (r.get("error") or ""), "错误信息说清原因",
       (r.get("error") or "")[:40])


async def t_round():
    print("\n[5] 轮询一轮：只有 status_code=8 才判离线")
    # 5.1 成功
    w = mkworker(FakeManager(acc()))
    w._verify_result = "ok"
    w.context = object()
    await w._run_round()
    ck(w.auth_state == "ok", "抓取成功 -> ok")
    ck(w.last_error == "", "无错误")

    # 5.2 不完整
    w2 = mkworker(FakeManager(acc(), fetch=lambda: {"__ok": True, "works": 1, "checked": 1,
                                                   "new": 0, "skipped": 0,
                                                   "complete": False}))
    w2._verify_result = "ok"
    w2.context = object()
    await w2._run_round()
    ck("本轮不完整" in w2.last_error, "不完整要记下来（不能当全量）", w2.last_error[:20])

    # 5.3 status_code=8 -> 判离线
    w3 = mkworker(FakeManager(acc(), fetch=lambda: {"__ok": False,
                                                   "__err": api.ERR_NOT_LOGGED_IN,
                                                   "__msg": "未登录"}))
    w3._verify_result = "ok"
    w3.context = object()
    await w3._run_round()
    ck(w3.auth_state == "offline", "code 8 -> 判离线")

    # 5.4 其它错误 -> 绝不判离线（误判会把好账号踢掉）
    for err in (api.ERR_EVAL, api.ERR_HTTP, "exception", "timeout", "not_ready"):
        w4 = mkworker(FakeManager(acc(), fetch=lambda e=err: {"__ok": False, "__err": e,
                                                             "__msg": "x"}))
        w4._verify_result = "ok"
        w4.context = object()
        await w4._run_round()
        ck(w4.auth_state != "offline", "错误 %s 不判离线" % err, w4.auth_state)

    # 5.5 上一轮已确认未登录 -> 零请求直接判离线
    hit = {"n": 0}

    def counting_fetch():
        hit["n"] += 1
        return {"__ok": True}
    w5 = mkworker(FakeManager(acc(), fetch=counting_fetch))
    w5._verify_result = "no"
    w5.context = object()
    await w5._run_round()
    ck(hit["n"] == 0, "已有未登录证据 -> 一个请求都不发", hit["n"])
    ck(w5.auth_state == "offline", "直接判离线")


async def t_persist():
    print("\n[6] 状态落盘 + 重新扫码后自动作废")
    tmp = os.path.join(tempfile.gettempdir(), "dy_auth_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    old = W.AUTH_FILE
    W.AUTH_FILE = tmp
    try:
        w = mk("S_OLD")
        w._remember_offline("掉线了")
        ck(os.path.exists(tmp), "文件已落盘")
        d = json.load(io.open(tmp, encoding="utf-8"))
        ck(d.get("dy_aaa", {}).get("reason") == "掉线了", "内容正确")

        # 重新构造一个 worker（模拟进程重启），同一身份 -> 仍是已知离线
        w2 = mk("S_OLD")
        ck(w2._is_known_offline() is True, "重启后仍认得离线（不白开浏览器）")

        # 用户重新扫码，sec_uid 变了 -> 离线记录作废
        w3 = mk("S_NEW")
        ck(w3._is_known_offline() is False, "重新扫码后旧离线记录自动作废")

        # 上线要清记录
        w4 = mk("S_OLD")
        w4._clear_offline()
        d2 = json.load(io.open(tmp, encoding="utf-8"))
        ck("dy_aaa" not in d2, "上线后记录被清掉")
    finally:
        W.AUTH_FILE = old
        if os.path.exists(tmp):
            os.remove(tmp)


async def t_loop_offline_no_browser():
    print("\n[7] 离线时轮询循环不开浏览器、不发包")
    w = mkworker(FakeManager(acc()))
    w.auth_state = "offline"
    opened = {"n": 0}
    fetched = {"n": 0}

    async def fake_open():
        opened["n"] += 1
        return True

    async def fake_fetch(*a, **k):
        fetched["n"] += 1
        return {"__ok": True}
    w._open = fake_open
    w.manager.fetch_now = fake_fetch
    task = asyncio.create_task(w._loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    ck(opened["n"] == 0, "没开浏览器", opened["n"])
    ck(fetched["n"] == 0, "没发请求", fetched["n"])


def t_login_profile_dir():
    print("\n[8] 登录会话：profile 目录不能是临时目录（铁律）")
    s = DyLoginSession(None, {}, _noop_emit)
    ck(s.acc_id.startswith("dy_"), "新账号 id 形如 dy_xxxxxx", s.acc_id)
    ck(len(s.acc_id) == 9, "id 长度 = 3 + 6", len(s.acc_id))
    ck("_login_" not in s.profile_dir, "不是 _login_* 临时目录", s.profile_dir)
    ck(s.profile_dir.endswith(s.acc_id), "目录名就是账号 id", s.profile_dir)
    s2 = DyLoginSession(None, {}, _noop_emit, account=acc(pdir="./profiles/keepme"))
    ck(s2.profile_dir == "./profiles/keepme", "重新登录沿用原 profile 目录")
    ck(_safe_name("张 三/abc!") == "张三abc", "名字做了清洗", _safe_name("张 三/abc!"))
    ck(_safe_name("") == "dy", "空名字有兜底")


async def t_login_finalize():
    print("\n[9] 登录 finalize：写出的账号字段")
    cfg = {"accounts": []}
    saved = {"n": 0}
    events = []

    async def emit(ev, payload):
        events.append((ev, payload))

    def save_config(c):
        saved["n"] += 1

    s = DyLoginSession(None, cfg, emit, save_config=save_config)
    s.identity = {"uid": "999", "sec_uid": "SEC_NEW", "unique_id": "un",
                  "nickname": "我的抖音号"}
    s._storage_state = {"cookies": [{"name": "sessionid", "value": "v"}]}
    await s._finalize()

    ck(len(cfg["accounts"]) == 1, "配置里多了一个账号")
    a = cfg["accounts"][0]
    ck(a["platform"] == "dy", "platform=dy", a.get("platform"))
    ck(a["_dy_sec_uid"] == "SEC_NEW", "sec_uid 已写入")
    ck(a["_dy_uid"] == "999", "uid 已写入")
    ck(a["name"] == "我的抖音号", "名字取自昵称", a.get("name"))
    ck("_login_" not in a["profile_dir"], "profile_dir 不是临时目录", a["profile_dir"])
    ck(a.get("_dy_storage_state", {}).get("cookies"), "storage_state 已留存（双保险）")
    ck(saved["n"] == 1, "落盘了一次", saved["n"])
    kinds = [e for e, _ in events]
    ck("login_status" in kinds and "accounts_updated" in kinds, "事件都推了", kinds)
    cap = [p for e, p in events if e == "login_status"][-1]
    ck(cap.get("status") == "captured", "状态 captured", cap.get("status"))
    ck(s.status == "captured", "会话状态 captured")

    # 重复 finalize 不再写
    await s._finalize()
    ck(len(cfg["accounts"]) == 1 and saved["n"] == 1, "重复 finalize 幂等")


async def t_login_dup():
    print("\n[10] 同一个抖音号重复扫码 -> 更新原账号，不新增")
    old_dir = "./profiles/dy_olddir"
    cfg = {"accounts": [dict(acc(aid="dy_old", sec="SEC_NEW", pdir=old_dir))]}
    events = []

    async def emit(ev, payload):
        events.append((ev, payload))

    s = DyLoginSession(None, cfg, emit, save_config=lambda c: None)
    s.identity = {"uid": "100", "sec_uid": "SEC_NEW", "unique_id": "u", "nickname": "同名"}
    await s._finalize()
    ck(len(cfg["accounts"]) == 1, "没有新增账号", len(cfg["accounts"]))
    ck(cfg["accounts"][0]["id"] == "dy_old", "沿用原账号 id")
    ck(cfg["accounts"][0]["profile_dir"] == s.profile_dir,
       "profile_dir 指向本次登录的新目录", cfg["accounts"][0]["profile_dir"])


async def t_close_not_selfcancel():
    print("\n[11] _close 不会把当前任务自己取消（rev56k 修的 bug）")
    s = DyLoginSession(None, {}, _noop_emit)
    reached = {"n": 0}

    async def body():
        # 把自己登记进 _tasks，再调 _close —— 正是 _finalize 的真实调用形态
        me = asyncio.current_task()
        s._tasks = [me]
        await s._close()
        reached["n"] += 1          # 如果自己被杀，这一行不会执行
        await asyncio.sleep(0)     # 再来一次 await，确认没有残留取消请求
        reached["n"] += 1

    task = asyncio.create_task(body())
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError) as e:
        print("      异常:", repr(e))
    ck(reached["n"] == 2, "_close 之后任务继续跑完", reached["n"])


async def t_capture_qr():
    print("\n[12] 二维码：优先直接取 img.src（实测就是 data URL）")

    class FakePage:
        def __init__(self, payload, handle=None):
            self.payload = payload
            self.calls = 0
            self._handle = handle

        async def evaluate(self, js):
            self.calls += 1
            return self.payload

        async def evaluate_handle(self, js):
            return self._handle

    data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    pg = FakePage(json.dumps({"w": 178, "h": 178, "src": data_url}))
    s = DyLoginSession(None, {}, _noop_emit)
    s.page = pg
    got = await s._capture_qr()
    ck(got == data_url, "直接返回 src（base64，不用截图）")
    ck(pg.calls == 1, "只求值一次")

    # src 为空 -> 走元素截图兜底
    class FakeEl:
        async def screenshot(self, type="png"):
            return b"\x89PNG" + b"x" * 400
    pg2 = FakePage("", handle=type("H", (), {"as_element": lambda self: FakeEl()})())
    s2 = DyLoginSession(None, {}, _noop_emit)
    s2.page = pg2
    got2 = await s2._capture_qr()
    ck(got2.startswith("data:image/png;base64,"), "兜底走元素截图（不截整页）")

    # 页面死了 -> 空串，不抛异常
    class DeadPage:
        async def evaluate(self, js):
            raise RuntimeError("page closed")

        async def evaluate_handle(self, js):
            raise RuntimeError("page closed")
    s3 = DyLoginSession(None, {}, _noop_emit)
    s3.page = DeadPage()
    ck(await s3._capture_qr() == "", "页面死了返回空串，不抛异常")


async def _noop_emit(ev, payload):
    return None


def main():
    # 全局隔离：所有用例写自己的临时状态文件，绝不碰 ./data/dy_auth_state.json
    tmp = os.path.join(tempfile.gettempdir(), "dy_auth_global_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    W.AUTH_FILE = tmp
    t_fp()
    asyncio.run(t_state_offline())
    asyncio.run(t_need_scan())
    asyncio.run(t_multi_account())
    asyncio.run(t_round())
    asyncio.run(t_persist())
    asyncio.run(t_loop_offline_no_browser())
    t_login_profile_dir()
    asyncio.run(t_login_finalize())
    asyncio.run(t_login_dup())
    asyncio.run(t_close_not_selfcancel())
    asyncio.run(t_capture_qr())
    print("\n" + "=" * 60)
    print("合计 PASS %d / FAIL %d" % (PASS, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
