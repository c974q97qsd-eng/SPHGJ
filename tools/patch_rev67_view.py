# -*- coding: utf-8 -*-
"""rev67: 直播大屏改为「访问者网页标签里 CDP 串流」,不再在服务端弹实体窗口。

背景(2026-11-06 用户反馈)
------------------------
用户从局域网另一台电脑的浏览器访问母本 WEB 界面,点「打开大屏」,
窗口却弹在母本(程序原型机)桌面上 —— open_dashboard / open_account_browser
是 launch_stealth(headless=False),永远在**服务端**那台机器弹窗,远程访问者看不到。

项目里已有正确答案:remote_view.py 开头就写了「WEB 端用户看不到本机窗口,
改成 CDP 串流」,/api/accounts/{id}/open-window 也带 _client_is_local 403 拦截。
本补丁让大屏复用这条已验证通道,而不是另造一套。

改动
----
server.py  : remote_ws 支持 view=dashboard(后端现取 liveObjectId 拼起始 URL),
             复用会话显式 goto + 放宽空闲超时;两个 headed 弹窗接口加 403 拦截;
             清理已过时的 docstring。
remote_view.py: 会话级 idle_timeout_sec(大屏 2 小时,普通串流仍 5 分钟)。
前端       : 大屏按钮 -> /remote?account=..&view=dashboard&from=/live-screen;
             RemotePage 透传 view、返回来源页、修「数据抓取已暂停」旧文案。
"""
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

# (相对路径, 原文, 新文, 标记, 期望命中次数)
PATCHES = [
    # ---------------- server.py ----------------
    (r"backend\server.py",
     "from .remote_view import RemoteRegistry\n",
     "from .remote_view import RemoteRegistry, DASHBOARD_IDLE_SEC\n",
     "S1 import DASHBOARD_IDLE_SEC", 1),

    (r"backend\server.py",
     '''@app.websocket("/api/remote/ws")
async def remote_ws(ws: WebSocket, account: str = Query(""), url: str = Query("")):
    """远程同步浏览通道(rev27)。

    文本帧 = JSON 指令/元信息;二进制帧 = JPEG 画面。
    客户端 -> 服务端:{t:"mouse"|"text"|"key"|"nav"|"ping"|"close"}
    服务端 -> 客户端:{t:"opening"|"hello"|"url"|"closed"|"error"} + 二进制 JPEG

    会话期间该账号 worker 会让出 profile 锁(数据抓取暂停),断开/空闲超时后自动恢复。
    """
    await ws.accept()
    if not account:
        try:
            await ws.send_json({"t": "error", "message": "缺少 account 参数"})
            await ws.close()
        except Exception:
            pass
        return
    sess = None
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        sess, reused = await remote_registry.open(account, url or None)
        await ws.send_json(sess.meta())
''',
     '''# rev67: 「打开直播大屏」不再在服务端弹实体窗口 —— 从别的电脑访问 WEB 界面时,
# 窗口会出现在运行程序的那台机器上(原型机),访问者根本看不到。改为和「远程查看」
# 共用 CDP 串流:由后端从运行中 worker 现取 liveObjectId 拼出起始地址,画面推到
# 访问者自己的网页标签里。
DASHBOARD_URL_TPL = ("https://channels.weixin.qq.com/platform/statistic/dashboardV4"
                     "?objetctId=%s&entrance_id=2")


def _dashboard_start_url(account_id):
    worker = manager.workers.get(account_id)
    lf = getattr(worker, "live_fetcher", None) if worker else None
    oid = getattr(lf, "live_object_id", None) if lf else None
    return DASHBOARD_URL_TPL % oid if oid else ""


@app.websocket("/api/remote/ws")
async def remote_ws(ws: WebSocket, account: str = Query(""), url: str = Query(""),
                    view: str = Query("")):
    """远程同步浏览通道(rev27 / rev67 支持 view=dashboard)。

    文本帧 = JSON 指令/元信息;二进制帧 = JPEG 画面。
    客户端 -> 服务端:{t:"mouse"|"text"|"key"|"nav"|"ping"|"close"}
    服务端 -> 客户端:{t:"opening"|"hello"|"url"|"closed"|"error"} + 二进制 JPEG

    rev66 起串流用独立临时 profile + 注入登录态,worker 一直在跑,数据抓取不中断。
    """
    await ws.accept()
    if not account:
        try:
            await ws.send_json({"t": "error", "message": "缺少 account 参数"})
            await ws.close()
        except Exception:
            pass
        return
    target_url = url or ""
    idle_sec = None
    if view == "dashboard":
        target_url = _dashboard_start_url(account)
        if not target_url:
            try:
                await ws.send_json({"t": "error",
                                    "message": "账号未在直播或未拿到 liveObjectId,打不开直播大屏"})
                await ws.close()
            except Exception:
                pass
            return
        idle_sec = DASHBOARD_IDLE_SEC
    sess = None
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        sess, reused = await remote_registry.open(account, target_url or None,
                                                   idle_timeout_sec=idle_sec)
        if reused:
            # registry 复用会话时不会重新 goto:同账号先开了「远程查看」再点「大屏」,
            # 画面会停在旧页。这里显式导航,并按大屏模式放宽空闲超时。
            if idle_sec and getattr(sess, "idle_timeout_sec", None) != idle_sec:
                sess.idle_timeout_sec = idle_sec
                sess.touch()
            if target_url:
                await sess.nav("goto", target_url)
        await ws.send_json(sess.meta())
''',
     "S2 remote_ws 支持 view=dashboard", 1),

    (r"backend\server.py",
     '''@app.post("/api/remote/close")
async def remote_close(account: str = Query("")):
    """手动断开某账号的串流并恢复抓取。"""
''',
     '''@app.post("/api/remote/close")
async def remote_close(account: str = Query("")):
    """手动断开某账号的串流(rev66 起 worker 从未停过,无需恢复采集)。"""
''',
     "S3 remote_close 文案", 1),

    (r"backend\server.py",
     '''@app.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(account_id: str):
    """打开带登录态的 headed 浏览器供手动操作(停 worker,关浏览器后自动重启)。"""
    ok = await manager.open_account_browser(account_id)
''',
     '''@app.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(account_id: str, request: Request):
    """在运行程序的本机打开带登录态的 headed 浏览器(rev66: 不停 worker)。

    rev67: 非本机访问一律 403 —— 窗口只会弹在服务端那台机器的桌面上,访问者看不到。
    远程请走 /remote 网页串流。
    """
    if not _client_is_local(request):
        raise HTTPException(403, "本机窗口只能在运行程序的这台机器上打开"
                                 "(远程请用「远程」按钮走网页串流)")
    ok = await manager.open_account_browser(account_id)
''',
     "S4 open-browser 非本机 403", 1),

    (r"backend\server.py",
     '''@app.post("/api/accounts/{account_id}/open-dashboard")
async def open_dashboard(account_id: str):
    """打开当前直播的 dashboardV4 大屏(需账号在直播,停 worker,关浏览器后自动重启)。"""
    ok, msg = await manager.open_dashboard(account_id)
''',
     '''@app.post("/api/accounts/{account_id}/open-dashboard")
async def open_dashboard(account_id: str, request: Request):
    """本机兼容入口:在运行程序的机器上弹 dashboardV4 实体窗口。

    rev67: WEB 端「打开大屏」已改为 /remote?view=dashboard 网页串流;这个接口只留给
    确实坐在运行机前的场景,非本机一律 403,避免又在原型机上弹出看不见的窗口。
    """
    if not _client_is_local(request):
        raise HTTPException(403, "本机窗口只能在运行程序的这台机器上打开"
                                 "(直播大屏请点界面上的「打开大屏」,走网页串流)")
    ok, msg = await manager.open_dashboard(account_id)
''',
     "S5 open-dashboard 非本机 403", 1),

    # ---------------- remote_view.py ----------------
    (r"backend\remote_view.py",
     "IDLE_TIMEOUT_SEC = 300          # 5 分钟无操作自动断开\n",
     "IDLE_TIMEOUT_SEC = 300          # 5 分钟无操作自动断开\n"
     "DASHBOARD_IDLE_SEC = 7200       # rev67: 直播大屏是挂着看的,2 小时无操作才断\n",
     "R1 大屏空闲常量", 1),

    (r"backend\remote_view.py",
     "    def __init__(self, manager, account_id, config, storage=None):\n",
     "    def __init__(self, manager, account_id, config, storage=None,\n"
     "                 idle_timeout_sec=IDLE_TIMEOUT_SEC):\n",
     "R2 会话级超时参数", 1),

    (r"backend\remote_view.py",
     '        self.url = ""\n'
     "        self.last_active = time.time()\n"
     "        self.created_at = time.time()\n",
     '        self.url = ""\n'
     "        self.last_active = time.time()\n"
     "        self.idle_timeout_sec = int(idle_timeout_sec or IDLE_TIMEOUT_SEC)\n"
     "        self.created_at = time.time()\n",
     "R3 会话级超时属性", 1),

    (r"backend\remote_view.py",
     "                if time.time() - self.last_active > IDLE_TIMEOUT_SEC + 10:\n",
     "                if time.time() - self.last_active > self.idle_timeout_sec + 10:\n",
     "R4 看门狗用会话超时", 1),

    (r"backend\remote_view.py",
     '                "idle_timeout": IDLE_TIMEOUT_SEC,\n',
     '                "idle_timeout": self.idle_timeout_sec,\n',
     "R5 meta 回报会话超时", 1),

    (r"backend\remote_view.py",
     "    async def open(self, account_id, url=None):\n",
     "    async def open(self, account_id, url=None, idle_timeout_sec=None):\n",
     "R6 registry.open 参数", 1),

    (r"backend\remote_view.py",
     "        s = RemoteSession(self.manager, account_id, self.config, self.storage)\n",
     "        s = RemoteSession(self.manager, account_id, self.config, self.storage,\n"
     "                          idle_timeout_sec=idle_timeout_sec or IDLE_TIMEOUT_SEC)\n",
     "R7 新建会话传超时", 1),

    # ---------------- LiveScreenPage.tsx ----------------
    (r"frontend\src\pages\LiveScreenPage.tsx",
     '''  const handleOpenDashboard = async () => {
    try {
      await api.openDashboard(it.account_id)
      toast.success("已打开直播大屏(不影响后台采集)")
    } catch (e) {
      toast.error("打开失败:" + (e as Error).message)
    }
  }
''',
     '''  // rev67: 大屏不再在「运行程序的那台机器」上弹实体窗口(远程访问根本看不到),
  // 改为和「远程查看」同一条 CDP 串流通道,画面直接显示在访问者的网页标签里。
  const handleOpenDashboard = () => {
    const target = `/remote?account=${encodeURIComponent(it.account_id)}` +
      `&view=dashboard&from=%2Flive-screen`
    const inWebview = !!(window as unknown as { pywebview?: unknown }).pywebview
    if (inWebview) {
      window.location.assign(target)
      return
    }
    const w = window.open(target, "_blank")
    if (!w) {
      toast.info("浏览器拦截了新标签页,已在当前页打开")
      window.location.assign(target)
    }
  }
''',
     "F1 大屏按钮改网页串流", 1),

    (r"frontend\src\pages\LiveScreenPage.tsx",
     'onClick={handleOpenDashboard} aria-label="打开直播大屏" title="打开直播大屏"',
     'onClick={handleOpenDashboard} aria-label="在网页标签里打开直播大屏" title="在网页标签里打开直播大屏"',
     "F2 按钮提示文案", 1),

    # ---------------- RemotePage.tsx ----------------
    (r"frontend\src\pages\RemotePage.tsx",
     '  const account = sp.get("account") || ""\n',
     '''  const account = sp.get("account") || ""
  const view = sp.get("view") || ""
  const rawFrom = sp.get("from") || "/accounts"
  const backTo = rawFrom.startsWith("/") && !rawFrom.startsWith("//") ? rawFrom : "/accounts"
''',
     "F3 读 view/from 参数", 1),

    (r"frontend\src\pages\RemotePage.tsx",
     '''    const ws = new WebSocket(
      `${proto}//${location.host}/api/remote/ws?account=${encodeURIComponent(account)}`)
''',
     '''    const extra = view ? `&view=${encodeURIComponent(view)}` : ""
    const ws = new WebSocket(
      `${proto}//${location.host}/api/remote/ws?account=${encodeURIComponent(account)}${extra}`)
''',
     "F4 WS 透传 view", 1),

    (r"frontend\src\pages\RemotePage.tsx",
     "  }, [account, onFrame])\n",
     "  }, [account, view, onFrame])\n",
     "F5 connect 依赖 view", 1),

    (r"frontend\src\pages\RemotePage.tsx",
     '        <Button size="sm" variant="ghost" className="gap-1" onClick={() => navigate("/accounts")}>\n',
     '        <Button size="sm" variant="ghost" className="gap-1" onClick={() => navigate(backTo)}>\n',
     "F6 关闭回来源页", 1),

    (r"frontend\src\pages\RemotePage.tsx",
     '              <Button size="sm" variant="secondary" onClick={() => navigate("/accounts")}>返回账号管理</Button>\n',
     '              <Button size="sm" variant="secondary" onClick={() => navigate(backTo)}>返回</Button>\n',
     "F7 异常/断开页返回按钮", 2),

    (r"frontend\src\pages\RemotePage.tsx",
     '''          {phase === "live"
            ? "串流期间该账号的数据抓取已暂停;断开或空闲超时会自动恢复"
            : "未连接"}
''',
     '''          {phase === "live"
            ? (view === "dashboard"
                ? "直播大屏串流中 · 独立浏览器 + 注入登录态,后台采集不受影响"
                : "串流使用独立浏览器 + 注入登录态,后台采集不受影响")
            : "未连接"}
''',
     "F8 底部状态文案", 1),

    # ---------------- api.ts ----------------
    (r"frontend\src\lib\api.ts",
     '''  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
  openDashboard: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-dashboard`, { method: "POST" }),
''',
     '''  // rev67: 这两个是「在运行程序的本机弹实体窗口」的兼容入口,远程访问会被 403。
  // WEB 界面请用 openAccountWindow(本机) 或 /remote?view=dashboard(网页串流)。
  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
  openDashboard: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-dashboard`, { method: "POST" }),
''',
     "F9 api.ts 标注兼容入口", 1),
]

CHECKS = [
    (r"backend\server.py", "def _dashboard_start_url(account_id):", True),
    (r"backend\server.py", "view: str = Query(\"\")", True),
    (r"backend\server.py", "idle_timeout_sec=idle_sec", True),
    (r"backend\server.py", "await sess.nav(\"goto\", target_url)", True),
    (r"backend\server.py", "raise HTTPException(403, \"本机窗口只能", 3),
    (r"backend\server.py", "会话期间该账号 worker 会让出 profile 锁", False),
    (r"backend\remote_view.py", "DASHBOARD_IDLE_SEC = 7200", True),
    (r"backend\remote_view.py", "self.idle_timeout_sec + 10", True),
    (r"backend\remote_view.py", '"idle_timeout": self.idle_timeout_sec', True),
    (r"frontend\src\pages\LiveScreenPage.tsx", "&view=dashboard&from=%2Flive-screen", True),
    (r"frontend\src\pages\LiveScreenPage.tsx", "api.openDashboard", False),
    (r"frontend\src\pages\RemotePage.tsx", "${extra}`)", True),
    (r"frontend\src\pages\RemotePage.tsx", "数据抓取已暂停", False),
    (r"frontend\src\pages\RemotePage.tsx", "navigate(backTo)", True),
]


def rel2abs(rel):
    return os.path.join(ROOT, rel.replace("\\", "/"))


def main():
    # 先把全部文件读入内存,统一成 LF 再替换,最后按各自原换行风格写回
    texts, styles = {}, {}
    for rel in dict.fromkeys(p[0] for p in PATCHES):
        p = rel2abs(rel)
        raw = io.open(p, "rb").read()
        styles[rel] = b"\r\n" in raw
        texts[rel] = raw.decode("utf-8").replace("\r\n", "\n")

    for rel, old, new, tag, cnt in PATCHES:
        s = texts[rel]
        got = s.count(old)
        assert got == cnt, "%s: 期望命中 %d 次,实际 %d 次" % (tag, cnt, got)
        texts[rel] = s.replace(old, new)
        print("  OK  %s" % tag)

    for rel, needle, want in CHECKS:
        s = texts[rel]
        if want is True:
            assert needle in s, "自校验失败(应存在): %s :: %s" % (rel, needle)
        elif want is False:
            assert needle not in s, "自校验失败(应消失): %s :: %s" % (rel, needle)
        else:
            assert s.count(needle) == want, \
                "自校验失败(次数): %s :: %s 期望 %s 实际 %s" % (
                    rel, needle, want, s.count(needle))
    print("  OK  自校验 %d 项" % len(CHECKS))

    for rel, s in texts.items():
        if styles[rel]:
            s = s.replace("\n", "\r\n")
        with io.open(rel2abs(rel), "w", encoding="utf-8", newline="") as f:
            f.write(s)
        print("  WRITE %s (%s)" % (rel, "CRLF" if styles[rel] else "LF"))
    print("PATCH PASS: %d 处补丁, %d 个文件" % (len(PATCHES), len(texts)))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("PATCH FAIL:", e)
        sys.exit(1)
