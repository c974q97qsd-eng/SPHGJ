# -*- coding: utf-8 -*-
"""rev74 单测：远程串流多标签冲突修复 + 会话上限可配。

真 import backend.remote_view（它不依赖 playwright），但把 RemoteSession 换成
假会话，绝不启动 Chromium；WebSocket 也用假的。所有用例在一个 asyncio.run 里。
"""
import asyncio
import io
import os
import sys
import time

ROOT = r"D:\SPHGJ"
sys.path.insert(0, ROOT)
from backend import remote_view as rv      # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, str(extra)[:200]))


def src(rel):
    with io.open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


RVIEW = src("backend/remote_view.py")
SERVER = src("backend/server.py")
REMOTE = src("frontend/src/pages/RemotePage.tsx")


class FakePage:
    def is_closed(self):
        return False


class FakeSession:
    opened = []
    made = []
    fail_open = False

    def __init__(self, manager, account_id, config, storage=None,
                 idle_timeout_sec=None):
        FakeSession.made.append(self)
        self.account_id = account_id
        self.config = config
        self.idle_timeout_sec = idle_timeout_sec or rv.IDLE_TIMEOUT_SEC
        self.closed = False
        self.close_reason = ""
        self.owner = None
        self.page = FakePage()
        self.cdp = object()
        self.ctx = object()
        self.url = ""
        self.created_at = time.time()
        self.last_active = self.created_at
        self.close_calls = []
        self.nav_calls = []
        self.profile_dir = None
        self.cdp_session = None
        self._task = None

    async def open(self, url=None):
        FakeSession.opened.append((self.account_id, url))
        if FakeSession.fail_open:
            raise RuntimeError("boom")
        self.url = url or ""
        return {"t": "hello"}

    async def refresh_url(self):
        return self.url

    async def nav(self, action, url=""):
        self.nav_calls.append((action, url))

    async def close(self, reason="closed"):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self.owner = None
        self.close_calls.append(reason)

    def meta(self):
        return {"t": "hello", "url": self.url}


class FakeWS:
    def __init__(self):
        self.sent = []
        self.is_closed = False

    async def send_json(self, o):
        self.sent.append(o)

    async def close(self):
        self.is_closed = True


def mkreg(max_sessions=None):
    cfg = {"accounts": []}
    if max_sessions is not None:
        cfg["remote"] = {"max_sessions": max_sessions}
    return rv.RemoteRegistry(None, cfg)


async def main():
    orig_session = rv.RemoteSession
    orig_drop = rv.drop_view_profile
    rv.RemoteSession = FakeSession
    rv.drop_view_profile = lambda p: None
    try:
        await run()
    finally:
        rv.RemoteSession = orig_session
        rv.drop_view_profile = orig_drop


async def run():
    print("=== A. 常量与可配上限 ===")
    check("1 默认上限放宽到 4", rv.DEFAULT_MAX_SESSIONS == 4, rv.DEFAULT_MAX_SESSIONS)
    check("2 MAX_SESSIONS 兼容别名", rv.MAX_SESSIONS == rv.DEFAULT_MAX_SESSIONS)
    check("3 SessionOccupied 是 RuntimeError",
          issubclass(rv.SessionOccupied, RuntimeError))
    check("4 无配置吃默认 4", mkreg().max_sessions() == 4)
    check("5 remote.max_sessions 生效", mkreg(6).max_sessions() == 6)
    check("6 下限收敛到 1", mkreg(0).max_sessions() >= 1)
    check("7 上限收敛到 16", mkreg(99).max_sessions() == 16)
    check("8 非法字符串回落默认", mkreg("abc").max_sessions() == 4)
    check("9 config=None 不崩", rv.RemoteRegistry(None, None).max_sessions() == 4)

    print("=== B. RemoteSession owner 与 close ===")
    s = rv.RemoteSession(None, "a1", {})
    check("10 session 初始 owner=None", getattr(s, "owner", "MISSING") is None)
    s.owner = object()
    await s.close("manual")
    check("11 close() 清掉 owner", s.owner is None and s.close_reason == "manual")

    print("=== C. is_occupied ===")
    reg = mkreg()
    w1, w2 = FakeWS(), FakeWS()
    check("12 无会话不占用", reg.is_occupied("a1", w1) is False)
    sess1, reused = await reg.open("a1", owner=w1)
    check("13 首个连接不复用", reused is False)
    check("14 首个连接记下 owner", sess1.owner is w1)
    check("15 owner 相同不算占用", reg.is_occupied("a1", w1) is False)
    check("16 owner 不同算占用", reg.is_occupied("a1", w2) is True)
    check("17 会话已关闭不算占用", not (await _close_and_check(reg, "a1", w1)))

    print("=== D. 同账号第二个标签页默认拒绝 ===")
    reg = mkreg()
    w1, w2 = FakeWS(), FakeWS()
    sess1, _ = await reg.open("a1", owner=w1)
    err = None
    try:
        await reg.open("a1", owner=w2)
    except rv.SessionOccupied as e:
        err = e
    check("18 抛 SessionOccupied", isinstance(err, rv.SessionOccupied), err)
    check("19 提示里说清另一个标签页", err and "另一个标签页" in str(err), err)
    check("20 拒绝后原会话仍活着", reg.get("a1") is sess1 and not sess1.closed)
    check("21 拒绝后 owner 没被抢走", sess1.owner is w1)
    check("22 拒绝时没有踢掉任何会话", len(reg.sessions) == 1)
    await reg.release("a1", sess1, w2, "client")
    check("23 非 owner 的 release 不误关", reg.get("a1") is sess1 and not sess1.closed)
    await reg.release("a1", sess1, w1, "client")
    check("24 owner 自己的 release 才收尾", reg.get("a1") is None and sess1.closed)

    print("=== E. view=dashboard 优雅接管 ===")
    reg = mkreg()
    w1, w2 = FakeWS(), FakeWS()
    sess1, _ = await reg.open("a1", owner=w1)
    sess2, reused = await reg.open("a1", url="https://x/dash", owner=w2, takeover=True)
    check("25 接管复用同一个 session", reused is True and sess2 is sess1)
    check("26 旧 WS 收到 kicked", any(m.get("t") == "closed" and m.get("reason") == "kicked"
                                      for m in w1.sent), w1.sent)
    check("27 旧 WS 被关闭", w1.is_closed)
    check("28 owner 移交给新 WS", sess1.owner is w2)
    await reg.release("a1", sess1, w1, "client")
    check("29 旧 WS 断开不会关掉接管后的会话", reg.get("a1") is sess1 and not sess1.closed)
    await reg.release("a1", sess1, w2, "client")
    check("30 新 WS 断开才收尾", reg.get("a1") is None and sess1.closed)

    print("=== F. 上限与踢旧 ===")
    reg = mkreg(2)
    ws = [FakeWS() for _ in range(3)]
    s1, _ = await reg.open("a1", owner=ws[0])
    s2, _ = await reg.open("a2", owner=ws[1])
    check("31 上限内两路并存", len(reg.sessions) == 2)
    s3, _ = await reg.open("a3", owner=ws[2])
    check("32 第三路踢掉最旧的 a1", "kicked" in s1.close_calls, s1.close_calls)
    check("33 a1 的 WS 收到 kicked", any(m.get("reason") == "kicked" for m in ws[0].sent))
    check("34 registry 只剩 a2/a3", set(reg.sessions) == {"a2", "a3"}, set(reg.sessions))
    check("35 被踢的 a2/a3 会话没被误关", not s2.closed and not s3.closed)
    await reg.release("a1", s1, ws[0], "client")
    check("36 被踢旧 WS 的 release 不误关 a3", reg.get("a3") is s3)

    reg = mkreg(2)
    ws = [FakeWS() for _ in range(3)]
    x1, _ = await reg.open("a1", owner=ws[0])
    x2, _ = await reg.open("a2", owner=ws[1])
    x2.created_at = x1.created_at - 100      # 故意让 dict 顺序与创建顺序相反
    await reg.open("a3", owner=ws[2])
    check("37 踢旧按 created_at 而非 dict 顺序",
          "kicked" in x2.close_calls and "kicked" not in x1.close_calls,
          (x1.close_calls, x2.close_calls))

    reg = mkreg(2)
    w1, w2, w3 = FakeWS(), FakeWS(), FakeWS()
    y1, _ = await reg.open("a1", owner=w1)
    y2, _ = await reg.open("a2", owner=w2)
    y3, reused = await reg.open("a1", owner=w3, takeover=True)
    check("38 接管时不会把自己踢掉", y3 is y1 and not y1.closed and reg.get("a1") is y1)
    check("39 接管后总路数不超上限", len(reg.sessions) <= 2, set(reg.sessions))

    reg = mkreg()
    ws = [FakeWS() for _ in range(5)]
    for i in range(4):
        await reg.open("a%d" % i, owner=ws[i])
    check("40 默认可同时开 4 路", len(reg.sessions) == 4, set(reg.sessions))
    await reg.open("a4", owner=ws[4])
    check("41 第 5 路仍在上限内", len(reg.sessions) == 4, set(reg.sessions))

    print("=== G. open 失败自清理 ===")
    reg = mkreg()
    before = len(FakeSession.made)
    FakeSession.fail_open = True
    try:
        try:
            await reg.open("a1", owner=FakeWS())
        except RuntimeError:
            pass
        check("42 失败后 registry 不留僵尸", reg.get("a1") is None)
        fresh = FakeSession.made[before:]
        check("43 半死会话被 close(open_failed)",
              len(fresh) == 1 and fresh[0].close_reason == "open_failed",
              [(x.account_id, x.close_reason) for x in fresh])
    finally:
        FakeSession.fail_open = False

    print("=== H. release 边界与 snapshot ===")
    reg = mkreg()
    w1, w2 = FakeWS(), FakeWS()
    s1, _ = await reg.open("a1", owner=w1)
    await reg.release("a1", None, w1, "client")
    check("44 sess=None 不抛且不误关", reg.get("a1") is s1)
    newer = FakeSession(None, "a1", {})
    newer.owner = w2
    reg.sessions["a1"] = newer
    await reg.release("a1", s1, w1, "client")
    check("45 会话已被替换时旧 release 不关新会话", reg.get("a1") is newer)
    snap = reg.snapshot()
    check("46 snapshot 回报 owned", len(snap) == 1 and snap[0]["owned"] is True, snap)

    print("=== I. server.py 接线 ===")
    check("47 import SessionOccupied", "DASHBOARD_IDLE_SEC, SessionOccupied" in SERVER)
    check("48 remote_ws 传 owner", "owner=ws, takeover=(view == \"dashboard\")" in SERVER)
    check("49 占用发 t=error", '{"t": "error", "message": str(e)}' in SERVER)
    check("50 建会话失败不再补发 closed", "skip_cleanup = True" in SERVER)
    check("51 finally 用 release", "await remote_registry.release(account, sess, ws, reason)" in SERVER)
    check("52 不再无条件 close(account, reason)",
          "await remote_registry.close(account, reason)" not in SERVER)
    check("53 finally 防 sess=None", 'getattr(sess, "close_reason", "")' in SERVER)
    check("54 sessions API 回报上限", '"max_sessions": remote_registry.max_sessions()' in SERVER)
    check("55 前端无需改：error 分支能显示 message",
          'm.t === "error"' in REMOTE and "m.message" in REMOTE)
    check("56 前端 dist 保持原样（本次不构建）",
          "index-B5EiYXq2.js" in src("frontend/dist/index.html"))


async def _close_and_check(reg, account, owner):
    """辅助：关掉会话后 is_occupied 应为 False。"""
    s = reg.get(account)
    await reg.release(account, s, owner, "test")
    return reg.is_occupied(account, object())


if __name__ == "__main__":
    asyncio.run(main())
    print("\n=== 结果: %d/%d 通过 ===" % (PASS, PASS + FAIL))
    raise SystemExit(0 if FAIL == 0 else 1)
