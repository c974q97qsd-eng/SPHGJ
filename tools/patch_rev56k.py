# -*- coding: utf-8 -*-
"""rev56k：修 rev56j 引入的三个逻辑漏洞（都会在真机上炸）。

1. **任务自杀式取消**（最严重）
   `_finalize()` 是由 `_wait_login_loop` 那个 task 调起来的，而 `_finalize` 里会
   `await self._close()`；`_close()` 又遍历 `self._tasks` 全部 cancel —— 结果把
   **当前正在执行的 task 自己**取消了，下一次 await 立刻抛 CancelledError，
   登录最后一步（写 config / 推事件）随机丢失。
   → `_close()` 必须跳过 `asyncio.current_task()`。

2. **need_scan 语义**
   原写法 `need_scan=True` 无条件是错的：浏览器都起不来（playwright 挂了 / profile
   被占）时也报「需要扫码」，用户会白白去扫一次码。只有**确定性未登录**才叫需要扫码。

3. **扫完码没人跑**
   登录成功后只写了 config，没有任何东西去开轮询 —— 用户扫完码界面还是不动。
   → 加 `on_finalized` 回调，服务器在登录成功后自动把 worker 拉起来。
   同时 `/api/dy/login/start` 支持传 account_id 做「重新登录」，并在登录前先停掉
   该账号的 worker（不然 profile 被占用，新 context 起不来）。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIN = os.path.join(ROOT, "backend", "douyin", "login.py")
WORKER = os.path.join(ROOT, "backend", "douyin", "worker.py")
SRV = os.path.join(ROOT, "backend", "server.py")

if "asyncio.current_task()" in io.open(LOGIN, encoding="utf-8").read():
    print("rev56k 已应用过，跳过。")
    sys.exit(0)


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次 tag=%s" % (os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


# ---------------- 1. login.py：_close 跳过当前 task + on_finalized ----------------
patch(LOGIN, [
    ("ctor", '''    def __init__(self, playwright, config, emit, account=None, save_config=None,
                 auto_finalize=True):''',
     '''    def __init__(self, playwright, config, emit, account=None, save_config=None,
                 auto_finalize=True, on_finalized=None):'''),
    ("ctor2", '''        self._save_config_cb = save_config
        self.auto_finalize = auto_finalize''',
     '''        self._save_config_cb = save_config
        self.auto_finalize = auto_finalize
        #: 登录成功落盘后的回调（服务器用它自动拉起 worker 开轮询）。
        #: 必须容错：回调炸了不能影响登录结果本身。
        self.on_finalized = on_finalized'''),
    ("close", '''    async def _close(self):
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks = []''',
     '''    async def _close(self):
        # 🚨 必须跳过当前 task：_finalize() 是在 _wait_login_loop 里被 await 的，
        # 它调 _close() 时如果把自己也 cancel 掉，下一次 await 就抛 CancelledError，
        # 登录最后一步（写 config / 推 captured 事件）会随机丢。
        cur = asyncio.current_task()
        for t in self._tasks:
            if t is cur:
                continue
            if not t.done():
                t.cancel()
        self._tasks = []'''),
    ("finalize-tail", '''        logger.info(f"[dylogin:{self.sid}] 抖音账号已保存: {acc['id']} ({acc.get('name')}) "
                    f"sec_uid={(acc.get('_dy_sec_uid') or '')[:16]}…")
        return acc''',
     '''        logger.info(f"[dylogin:{self.sid}] 抖音账号已保存: {acc['id']} ({acc.get('name')}) "
                    f"sec_uid={(acc.get('_dy_sec_uid') or '')[:16]}…")
        # 扫完码得有人接着跑：交给服务器把 worker 拉起来
        if self.on_finalized:
            try:
                await self.on_finalized(acc)
            except Exception as e:
                logger.warning(f"[dylogin:{self.sid}] on_finalized 回调异常(忽略): {e!r}")
        return acc'''),
])

# ---------------- 2. worker.py：need_scan 只看确定性未登录 ----------------
patch(WORKER, [
    ("needscan", '''        ok = await self._open()
        if not ok:
            return {"ok": False, "offline": self.auth_state == "offline",
                    "need_scan": True, "msg": self.auth_reason or self.last_error}''',
     '''        ok = await self._open()
        if not ok:
            # 只有**确定性未登录**才叫「需要扫码」。浏览器起不来 / 网络不通
            # 只是「没验成」，让用户去扫码是白折腾（他也扫不出来）。
            sure_offline = (self.auth_state == "offline")
            return {"ok": False, "offline": sure_offline, "need_scan": sure_offline,
                    "msg": self.auth_reason or self.last_error}'''),
])

# ---------------- 3. server.py：登录成功后自动拉起 worker ----------------
patch(SRV, [
    ("after-hook", '''def _dy_worker(account_id=None):''',
     '''async def _dy_after_login(acc):
    """登录成功落盘后自动把该账号跑起来（开浏览器 + 起轮询）。

    放在后台任务里：扫码会话此刻正在收尾，别让开浏览器（几秒）卡住它。
    """
    aid = (acc or {}).get("id") or ""
    if not aid:
        return

    async def _run():
        try:
            w = _dy_worker(aid)
            if w is None:
                return
            res = await w.start(playwright=await _dy_pw(), force=True)
            logger.info("[dy:%s] 登录后自动启动: %s" % (aid, res))
        except Exception as e:
            logger.warning("[dy:%s] 登录后自动启动异常: %r" % (aid, e))

    asyncio.create_task(_run())


def _dy_worker(account_id=None):'''),
    ("start-route", '''@app.post("/api/dy/login/start")
async def dy_login_start(headed: Optional[bool] = Query(None)):
    """开始抖音扫码登录。返回 sid，二维码通过 qr_update 事件推送（也可轮询本接口）。"""
    if headed is None:
        headed = _login_popup_window()
    pw = await _dy_pw()
    sess = DyLoginSession(pw, config, _dy_emit, save_config=save_config)
    _dy_login_sessions[sess.sid] = sess
    await sess.start(headed=headed)
    return {"sid": sess.sid, "status": sess.status, "headed": headed, "platform": "dy"}''',
     '''@app.post("/api/dy/login/start")
async def dy_login_start(headed: Optional[bool] = Query(None),
                         account_id: Optional[str] = Query(None)):
    """开始抖音扫码登录。返回 sid，二维码通过 qr_update 事件推送（也可轮询本接口）。

    account_id 有值 = 给已有账号**重新登录**（沿用它的 id 与 profile 目录）。
    重新登录前必须先停掉该账号的 worker —— 否则 profile 被运行中的 context 独占，
    新 context 根本起不来（这也是视频号那边反复踩过的坑）。
    """
    if headed is None:
        headed = _login_popup_window()
    acc = None
    if account_id:
        acc = _dy_account(account_id)
        if not acc:
            raise HTTPException(404, "抖音账号不存在")
        w = _dy_workers.get(account_id)
        if w and w.status().get("running"):
            await w.stop()
            logger.info("[dy:%s] 重新登录前已停止运行中的 worker" % account_id)
    pw = await _dy_pw()
    sess = DyLoginSession(pw, config, _dy_emit, account=acc,
                          save_config=save_config, on_finalized=_dy_after_login)
    _dy_login_sessions[sess.sid] = sess
    await sess.start(headed=headed)
    return {"sid": sess.sid, "status": sess.status, "headed": headed,
            "platform": "dy", "account_id": sess.acc_id}'''),
])


# ---------------- 自校验 ----------------
def selfcheck():
    lg = io.open(LOGIN, encoding="utf-8").read()
    wk = io.open(WORKER, encoding="utf-8").read()
    sv = io.open(SRV, encoding="utf-8").read()
    checks = [
        ("_close 跳过当前 task", "if t is cur:" in lg),
        ("_close 取到 current_task", "cur = asyncio.current_task()" in lg),
        ("有 on_finalized 参数", "on_finalized=None):" in lg),
        ("finalize 末尾回调", "await self.on_finalized(acc)" in lg),
        ("need_scan 只在确定离线", "sure_offline = (self.auth_state == \"offline\")" in wk),
        ("need_scan 已绑定 sure_offline", '"need_scan": sure_offline' in wk),
        ("server 有 after_login 钩子", "async def _dy_after_login(acc):" in sv),
        ("登录路由支持 account_id", "account_id: Optional[str] = Query(None)):" in sv),
        ("重登前停 worker", "重新登录前已停止运行中的 worker" in sv),
        ("会话带 on_finalized", "on_finalized=_dy_after_login" in sv),
    ]
    bad = 0
    for n, ok in checks:
        print(("  PASS  " if ok else "  FAIL  ") + n)
        bad += 0 if ok else 1
    return bad


print("=== 自校验 ===")
bad = selfcheck()

print("=== 语法检查 ===")
import py_compile  # noqa: E402
for p in (LOGIN, WORKER, SRV):
    try:
        py_compile.compile(p, doraise=True)
        print("  OK   " + os.path.basename(p))
    except Exception as e:
        bad += 1
        print("  FAIL " + os.path.basename(p) + " : " + str(e)[:200])

print("\n结果:", "ALL PASS" if not bad else "有 %d 项失败" % bad)
raise SystemExit(0 if not bad else 1)
