# -*- coding: utf-8 -*-
"""rev56j：抖音登录 + 浏览器生命周期接线。

三件事：
  1. DouyinManager 加 verify_login() —— 供 worker 做「确定性未登录」判定。
     必须是独立方法：refresh_identity() 在「登录成功但一个作品都没有」的新号上
     返回 {}，跟「未登录」长得一模一样，用它判离线会把新号直接踢掉。
  2. server.py 加 4 条登录路由 + 2 条生命周期路由 + 1 条 worker 状态路由。
  3. 启动后自动拉起抖音账号（有 dy 账号才动，没有就完全无操作）。

幂等：检测到已应用就跳过。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MGR = os.path.join(ROOT, "backend", "douyin", "manager.py")
SRV = os.path.join(ROOT, "backend", "server.py")

if os.path.exists(SRV) and "DyLoginSession" in io.open(SRV, encoding="utf-8").read():
    print("rev56j 已应用过，跳过。")
    sys.exit(0)


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次 tag=%s" % (os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


# ---------------- manager：verify_login ----------------
VERIFY = '''    async def verify_login(self):
        """硬验登录态。返回 {"ok": bool, "reason": str, "identity": dict}。

        🚨 判据只有一条：work_list 真的打通了（api.judge 通过 = 响应里有 extra）。
        **不能**拿 refresh_identity() 当判定 —— 它对「登录成功但零作品」的新号
        返回 {}，和「未登录」完全同形，用它判离线会把新号直接踢掉。

        失败时区分三种 reason，上层据此决定要不要判离线：
          · status_code=8 -> api.ERR_NOT_LOGGED_IN（**确定未登录**，可以判离线）
          · 其它（超时 / -1 / 页面死）-> 只算「没验成」，一律不判离线
        """
        client, why = self._need_client()
        if client is None:
            return {"ok": False, "reason": "not_ready", "identity": {}}
        env = await client.fetch_work_page(max_cursor=0, count=1)
        if not env.get("__ok"):
            return {"ok": False, "reason": env.get("__err") or "fail", "identity": {},
                    "status_code": env.get("status_code")}
        ident = {}
        for w in (env.get("works") or []):
            a = w.get("author") or {}
            if a.get("uid") or a.get("sec_uid"):
                ident = {"uid": str(a.get("uid") or ""),
                         "sec_uid": str(a.get("sec_uid") or ""),
                         "unique_id": str(a.get("unique_id") or ""),
                         "nickname": a.get("nickname") or ""}
                break
        if ident:
            self._identity = ident
        return {"ok": True, "reason": "", "identity": ident}

    # ---------------- 抓取 ----------------'''

patch(MGR, [
    ("verify", "    # ---------------- 抓取 ----------------", VERIFY),
])

# ---------------- server ----------------
IMPORT_OLD = "from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）"
IMPORT_NEW = ("from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）\n"
              "from .douyin.login import DyLoginSession     # rev56j: 抖音扫码登录\n"
              "from .douyin.worker import DouyinWorker      # rev56j: 抖音浏览器生命周期")

GLOBALS_OLD = "_dy_cdp = {}   # {pw, browser} —— 挂载的外部浏览器句柄，别让 GC 收掉"
GLOBALS_NEW = ("_dy_cdp = {}   # {pw, browser} —— 挂载的外部浏览器句柄，别让 GC 收掉\n"
               "_dy_login_sessions = {}   # sid -> DyLoginSession\n"
               "_dy_workers = {}          # account_id -> DouyinWorker")

HELPER_OLD = "# ===================== 评论(归属 = 视频号 finder_id,不是登录卡片) ====================="
HELPER_NEW = '''# ===================== 抖音：扫码登录 / 浏览器生命周期 =====================
async def _dy_emit(event, payload):
    try:
        await hub.emit(event, payload)
    except Exception as e:
        logger.debug("抖音事件推送失败 %s: %r" % (event, e))


async def _dy_pw():
    """复用 AccountManager 的 playwright 实例。

    同一个 playwright 进程能跑多个浏览器 context，没必要再起一套（省一份内存，
    也让「服务停止时一起收尾」只有一处逻辑）。
    """
    return await manager._ensure_playwright()


def _dy_accounts():
    return [a for a in config.get("accounts", []) if platforms.is_douyin(a)]


def _dy_account(account_id=None):
    accs = _dy_accounts()
    if account_id:
        return next((a for a in accs if a.get("id") == account_id), None)
    return accs[0] if accs else None


def _dy_worker(account_id=None):
    """取（或建）该账号的 worker。没有该账号返回 None。"""
    acc = _dy_account(account_id)
    if not acc:
        return None
    aid = acc.get("id") or ""
    w = _dy_workers.get(aid)
    if w is None:
        w = DouyinWorker(acc, config, storage, dy_manager, emit=_dy_emit)
        _dy_workers[aid] = w
    return w


@app.post("/api/dy/login/start")
async def dy_login_start(headed: Optional[bool] = Query(None)):
    """开始抖音扫码登录。返回 sid，二维码通过 qr_update 事件推送（也可轮询本接口）。"""
    if headed is None:
        headed = _login_popup_window()
    pw = await _dy_pw()
    sess = DyLoginSession(pw, config, _dy_emit, save_config=save_config)
    _dy_login_sessions[sess.sid] = sess
    await sess.start(headed=headed)
    return {"sid": sess.sid, "status": sess.status, "headed": headed, "platform": "dy"}


@app.get("/api/dy/login/{sid}")
async def dy_login_state(sid: str):
    """登录会话快照。image 字段给「不订阅 WS 也想拿二维码」的调用方用。"""
    s = _dy_login_sessions.get(sid)
    if not s:
        raise HTTPException(404, "抖音登录会话不存在")
    d = s.snapshot()
    if s.qr_image:
        d["image"] = s.qr_image
    if s.status in ("captured", "failed", "cancelled"):
        _dy_login_sessions.pop(sid, None)
    return d


@app.post("/api/dy/login/{sid}/cancel")
async def dy_login_cancel(sid: str):
    s = _dy_login_sessions.pop(sid, None)
    if not s:
        raise HTTPException(404, "抖音登录会话不存在")
    await s.cancel()
    return {"ok": True}


@app.get("/api/dy/workers")
async def dy_worker_list():
    return {"workers": [w.status() for w in _dy_workers.values()],
            "accounts": [platforms.account_brief(a) for a in _dy_accounts()]}


@app.post("/api/dy/accounts/{account_id}/start")
async def dy_account_start(account_id: str, force: bool = Query(False)):
    """启动抖音账号（开浏览器 + 硬验 + 起轮询）。

    未登录时返回 need_scan=True，前端据此走扫码流程 —— 这与视频号 rev55 的教训一致：
    「账号不存在」和「已判掉线跳过启动」是两种语义，混成 404 会误导前端。
    """
    acc = _dy_account(account_id)
    if not acc:
        raise HTTPException(404, "抖音账号不存在")
    w = _dy_worker(account_id)
    res = await w.start(playwright=await _dy_pw(), force=force)
    if not res.get("ok"):
        return {"ok": False, "need_scan": bool(res.get("need_scan")),
                "offline": bool(res.get("offline")),
                "msg": res.get("msg") or res.get("error") or "", "worker": w.status()}
    return {"ok": True, "worker": w.status()}


@app.post("/api/dy/accounts/{account_id}/stop")
async def dy_account_stop(account_id: str):
    w = _dy_workers.get(account_id)
    if not w:
        return {"ok": True, "msg": "该账号本来就未运行"}
    await w.stop()
    return {"ok": True, "worker": w.status()}


# ===================== 评论(归属 = 视频号 finder_id,不是登录卡片) ====================='''

patch(SRV, [
    ("import", IMPORT_OLD, IMPORT_NEW),
    ("globals", GLOBALS_OLD, GLOBALS_NEW),
    ("routes", HELPER_OLD, HELPER_NEW),
])

# 启动时自动拉起（有 dy 账号才动）
STARTUP_OLD = '''    # rev31: 清掉上次进程遗留的本机窗口临时 profile(_view_*)'''
STARTUP_NEW = '''    # rev56j: 自动拉起抖音账号（有 platform=dy 的账号才动；没有就完全无操作）
    async def _dy_auto_start():
        await asyncio.sleep(25)
        if config.get("dy_auto_start", True) is False:
            logger.info("[dy] dy_auto_start=false，跳过自动启动")
            return
        accs = [a for a in config.get("accounts", []) if platforms.is_douyin(a)]
        if not accs:
            return
        for acc in accs:
            try:
                w = _dy_worker(acc.get("id"))
                if w is None:
                    continue
                res = await w.start(playwright=await _dy_pw())
                logger.info("[dy:%s] 自动启动: %s" % (acc.get("id"), res))
            except Exception as e:
                logger.warning("[dy:%s] 自动启动异常: %r" % (acc.get("id"), e))
    asyncio.create_task(_dy_auto_start())
    # rev31: 清掉上次进程遗留的本机窗口临时 profile(_view_*)'''
patch(SRV, [("startup", STARTUP_OLD, STARTUP_NEW)])


# ---------------- 自校验 ----------------
def selfcheck():
    m = io.open(MGR, encoding="utf-8").read()
    s = io.open(SRV, encoding="utf-8").read()
    checks = [
        ("manager 有 verify_login", "async def verify_login(self):" in m),
        ("manager 不用 refresh_identity 判登录", "不能**拿 refresh_identity()" in m),
        ("server 导入登录会话", "from .douyin.login import DyLoginSession" in s),
        ("server 导入 worker", "from .douyin.worker import DouyinWorker" in s),
        ("server 有登录路由", '@app.post("/api/dy/login/start")' in s),
        ("server 有登录快照路由", '@app.get("/api/dy/login/{sid}")' in s),
        ("server 有取消路由", '@app.post("/api/dy/login/{sid}/cancel")' in s),
        ("server 有启动路由", '@app.post("/api/dy/accounts/{account_id}/start")' in s),
        ("server 有停止路由", '@app.post("/api/dy/accounts/{account_id}/stop")' in s),
        ("server 有 worker 状态路由", '@app.get("/api/dy/workers")' in s),
        ("need_scan 不混成 404", '"need_scan": bool(res.get("need_scan"))' in s),
        ("启动自动拉起", "_dy_auto_start" in s),
        ("复用同一个 playwright", "manager._ensure_playwright()" in s),
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
for p in (MGR, SRV, os.path.join(ROOT, "backend", "douyin", "login.py"),
          os.path.join(ROOT, "backend", "douyin", "worker.py")):
    try:
        py_compile.compile(p, doraise=True)
        print("  OK   " + os.path.basename(p))
    except Exception as e:
        bad += 1
        print("  FAIL " + os.path.basename(p) + " : " + str(e)[:200])

print("\n结果:", "ALL PASS" if not bad else "有 %d 项失败" % bad)
raise SystemExit(0 if not bad else 1)
