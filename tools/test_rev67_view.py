# -*- coding: utf-8 -*-
"""rev67 单测：直播大屏走访问者网页串流,服务端不再弹实体窗口。

server.py 会起服务/读 config,不能直接 import —— 用 AST 把新增函数抽到受控
namespace 里 exec(缺任何依赖就是 AttributeError,等于强断言)。
remote_view.py 不依赖 playwright,直接 import 真模块跑。
"""
import ast
import asyncio
import io
import os
import re
import sys
from types import SimpleNamespace

ROOT = r"D:\SPHGJ"
sys.path.insert(0, ROOT)
from backend import remote_view as rv      # noqa: E402

RESULT = []


def check(name, cond, extra=""):
    RESULT.append((name, bool(cond), extra))
    print("  %s %s%s" % ("OK " if cond else "BAD", name,
                         "" if cond else "  <- " + str(extra)[:180]))


def src(rel):
    with io.open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


SERVER = src("backend/server.py")
RVIEW = src("backend/remote_view.py")
LIVE = src("frontend/src/pages/LiveScreenPage.tsx")
REMOTE = src("frontend/src/pages/RemotePage.tsx")
API = src("frontend/src/lib/api.ts")

OID = "LIVE-OBJ-123"
URL_TPL = ("https://channels.weixin.qq.com/platform/statistic/dashboardV4"
           "?objetctId=%s&entrance_id=2")
DASH_URL = URL_TPL % OID


class Log:
    def warning(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def debug(self, *a, **kw): pass
    def error(self, *a, **kw): pass


class HTTPException(Exception):
    def __init__(self, code, detail=""):
        super().__init__(detail)
        self.status_code = code
        self.detail = detail


class Request:
    pass


class FakeManager:
    def __init__(self, oid=OID, worker=True, with_fetcher=True):
        if worker and with_fetcher:
            lf = SimpleNamespace(live_object_id=oid)
            self.workers = {"a1": SimpleNamespace(live_fetcher=lf)}
        elif worker:
            self.workers = {"a1": SimpleNamespace(live_fetcher=None)}
        else:
            self.workers = {}
        self.dash_calls, self.browser_calls = [], []

    async def open_dashboard(self, aid):
        self.dash_calls.append(aid)
        return True, ""

    async def open_account_browser(self, aid):
        self.browser_calls.append(aid)
        return True


class FakeSession:
    def __init__(self, url=""):
        self.url = url
        self.idle_timeout_sec = 300
        self.nav_calls = []
        self.touch_calls = 0
        self.close_reason = ""
        self.closed = False

    async def nav(self, action, url=""):
        self.nav_calls.append((action, url))

    def touch(self):
        self.touch_calls += 1

    def meta(self):
        return {"t": "hello", "url": self.url,
                "idle_timeout": self.idle_timeout_sec}


class FakeRegistry:
    def __init__(self, reused=False, boom=False):
        self.reused, self.boom = reused, boom
        self.calls, self.closed = [], []
        self.session = FakeSession()

    async def open(self, account, url=None, idle_timeout_sec=None):
        self.calls.append((account, url, idle_timeout_sec))
        if self.boom:
            raise RuntimeError("boom")
        if not self.reused and idle_timeout_sec:
            self.session.idle_timeout_sec = idle_timeout_sec
        self.session.url = url or ""
        return self.session, self.reused

    async def close(self, account, reason):
        self.closed.append((account, reason))


class FakeWS:
    def __init__(self):
        self.sent, self.is_closed = [], False

    async def accept(self):
        pass

    async def send_json(self, o):
        self.sent.append(o)

    async def close(self):
        self.is_closed = True


def build_open_part(manager, registry):
    """把 remote_ws 的前半段(到 hello 发出为止)抽成可独立调用的协程。"""
    i = SERVER.index("async def remote_ws(")
    a = SERVER.index("    await ws.accept()\n", i)
    # 必须截到内部 reader() 定义之前:hello 之后还有建会话失败的 except 块,
    # 只截到 hello 会留下没有 except 的 try,SyntaxError。
    b = SERVER.index("    async def reader():", a)
    body = SERVER[a:b]
    code = "async def open_part(ws, account, url, view):\n" + body

    ns = {"manager": manager, "remote_registry": registry, "logger": Log(),
          "DASHBOARD_IDLE_SEC": rv.DASHBOARD_IDLE_SEC}
    tree = ast.parse(SERVER)
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_dashboard_start_url":
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "DASHBOARD_URL_TPL" for t in node.targets):
            picked.append(node)
    assert len(picked) == 2, "server.py 里应能抽到 URL 模板与 _dashboard_start_url"
    mod = ast.Module(body=picked, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), "<server_consts>", "exec"), ns)
    exec(compile(code, "<remote_ws_open>", "exec"), ns)
    return ns["open_part"], ns


def build_routes(manager, local):
    ns = {"manager": manager, "_client_is_local": lambda r: local,
          "HTTPException": HTTPException, "Request": Request, "logger": Log()}
    tree = ast.parse(SERVER)
    for name in ("open_dashboard", "open_account_browser"):
        fn = next((n for n in tree.body
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == name), None)
        assert fn is not None, "找不到路由 " + name
        # 装饰器 @app.post(...) 在受控 namespace 里没有 app,去掉只留函数体
        import copy
        fn = copy.deepcopy(fn)
        fn.decorator_list = []
        mod = ast.Module(body=[fn], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), "<route>", "exec"), ns)
    return ns


async def run_open(view="dashboard", url="", oid=OID, worker=True,
                   with_fetcher=True, reused=False, boom=False):
    manager = FakeManager(oid=oid, worker=worker, with_fetcher=with_fetcher)
    registry = FakeRegistry(reused=reused, boom=boom)
    fn, ns = build_open_part(manager, registry)
    ws = FakeWS()
    await fn(ws, "a1", url, view)
    return ws, registry, ns


async def main():
    print("=== A. 大屏起始地址 ===")
    fn, ns = build_open_part(FakeManager(), FakeRegistry())
    check("1 从运行中 worker 取 liveObjectId 拼 URL",
          ns["_dashboard_start_url"]("a1") == DASH_URL,
          ns["_dashboard_start_url"]("a1"))
    check("2 URL 保留官方 objetctId 拼写与 entrance_id",
          "objetctId=" in DASH_URL and "entrance_id=2" in DASH_URL)
    for tag, kw, label in [
        ("3 无 worker", dict(worker=False), "no worker"),
        ("4 worker 无 live_fetcher", dict(with_fetcher=False), "no fetcher"),
        ("5 liveObjectId 为空", dict(oid=""), "empty oid"),
    ]:
        f2, n2 = build_open_part(FakeManager(**kw), FakeRegistry())
        check(tag + " 返回空串", n2["_dashboard_start_url"]("a1") == "", label)

    print("=== B. remote_ws view=dashboard ===")
    ws, reg, _ = await run_open()
    check("6 新建会话传 dashboard URL + 2 小时超时",
          reg.calls == [("a1", DASH_URL, 7200)], reg.calls)
    check("7 发出 opening 后发出 hello",
          [m.get("t") for m in ws.sent] == ["opening", "hello"], ws.sent)
    check("8 hello 里回报 7200",
          ws.sent[-1]["idle_timeout"] == 7200, ws.sent[-1])
    check("9 新建会话不做重复 goto", reg.session.nav_calls == [], reg.session.nav_calls)

    ws, reg, _ = await run_open(reused=True)
    check("10 复用会话显式 goto 大屏",
          reg.session.nav_calls == [("goto", DASH_URL)], reg.session.nav_calls)
    check("11 复用会话超时提升到 7200 并重置计时",
          reg.session.idle_timeout_sec == 7200 and reg.session.touch_calls >= 1,
          (reg.session.idle_timeout_sec, reg.session.touch_calls))
    check("12 复用时 hello 回报新超时",
          ws.sent[-1]["idle_timeout"] == 7200, ws.sent[-1])

    for tag, kw in [
        ("13 无 worker", dict(worker=False)),
        ("14 无 liveObjectId", dict(oid="")),
    ]:
        ws, reg, _ = await run_open(**kw)
        check(tag + " 给明确 error 且不建会话",
              reg.calls == [] and ws.sent[-1].get("t") == "error"
              and "liveObjectId" in ws.sent[-1].get("message", "")
              and ws.is_closed, (reg.calls, ws.sent, ws.is_closed))

    ws, reg, _ = await run_open(view="", url="")
    check("15 普通远程查看不变(url=None, 默认超时)",
          reg.calls == [("a1", None, None)] and reg.session.nav_calls == [],
          (reg.calls, reg.session.nav_calls))
    ws, reg, _ = await run_open(view="", url="https://example.com/x")
    check("16 显式 url 仍透传", reg.calls == [("a1", "https://example.com/x", None)], reg.calls)
    ws, reg, _ = await run_open(boom=True)
    check("17 建会话失败清僵尸会话并发 error",
          any(r[1] == "open_failed" for r in reg.closed)
          and ws.sent[-1].get("t") == "error" and ws.is_closed,
          (reg.closed, ws.sent))

    print("=== C. 非本机不再弹实体窗口 ===")
    m = FakeManager()
    ns = build_routes(m, local=False)
    try:
        await ns["open_dashboard"]("a1", Request())
        got = "no exception"
    except HTTPException as e:
        got = e.status_code
    check("18 远程点旧 open-dashboard -> 403", got == 403, got)
    check("19 403 时不碰 manager(不弹窗)", m.dash_calls == [], m.dash_calls)
    try:
        await ns["open_account_browser"]("a1", Request())
        got = "no exception"
    except HTTPException as e:
        got = e.status_code
    check("20 远程点旧 open-browser -> 403", got == 403, got)
    check("21 403 时不碰 manager(不弹窗)", m.browser_calls == [], m.browser_calls)

    m2 = FakeManager()
    ns2 = build_routes(m2, local=True)
    r = await ns2["open_dashboard"]("a1", Request())
    check("22 本机仍可弹窗(兼容入口保留)", r == {"ok": True} and m2.dash_calls == ["a1"], r)
    r = await ns2["open_account_browser"]("a1", Request())
    check("23 本机 open-browser 仍可用", r == {"ok": True} and m2.browser_calls == ["a1"], r)

    print("=== D. remote_view 会话级空闲超时 ===")
    check("24 DASHBOARD_IDLE_SEC = 7200", rv.DASHBOARD_IDLE_SEC == 7200)
    s = rv.RemoteSession(FakeManager(), "a1", {"accounts": []})
    check("25 普通会话仍是 5 分钟", s.idle_timeout_sec == 300, s.idle_timeout_sec)
    s2 = rv.RemoteSession(FakeManager(), "a1", {"accounts": []},
                          idle_timeout_sec=rv.DASHBOARD_IDLE_SEC)
    check("26 大屏会话 2 小时", s2.idle_timeout_sec == 7200, s2.idle_timeout_sec)
    check("27 meta 回报会话自己的超时", s2.meta()["idle_timeout"] == 7200, s2.meta())
    check("28 看门狗用 self.idle_timeout_sec 而非常量",
          "self.idle_timeout_sec + 10" in RVIEW
          and "IDLE_TIMEOUT_SEC + 10" not in RVIEW)

    made = []

    class ProbeSession:
        def __init__(self, manager, account_id, config, storage=None,
                     idle_timeout_sec=None):
            made.append(idle_timeout_sec)
            self.closed = False
            self.url = ""

        async def open(self, url=None):
            return {"t": "hello"}

        def meta(self):
            return {"t": "hello"}

    orig = rv.RemoteSession
    rv.RemoteSession = ProbeSession
    try:
        reg = rv.RemoteRegistry(FakeManager(), {"accounts": []})
        await reg.open("a1", DASH_URL, idle_timeout_sec=7200)
        await reg.open("a2", None)
    finally:
        rv.RemoteSession = orig
    check("29 registry 把两种超时分别传给会话", made == [7200, 300], made)

    print("=== E. 前端与 dist ===")
    check("30 大屏按钮不再调 open-dashboard", "api.openDashboard" not in LIVE)
    check("31 大屏按钮跳 /remote?view=dashboard",
          "&view=dashboard&from=%2Flive-screen" in LIVE)
    check("32 大屏按钮在网页标签打开(window.open)",
          'window.open(target, "_blank")' in LIVE)
    check("33 RemotePage 把 view 透传进 WS", "&view=${encodeURIComponent(view)}" in REMOTE)
    check("34 RemotePage 关闭回来源页", "navigate(backTo)" in REMOTE)
    check("35 RemotePage 不再宣称抓取已暂停", "数据抓取已暂停" not in REMOTE)
    check("36 api.ts 标注两个兼容入口", "rev67" in API)
    check("37 server.py 不再写「让出 profile 锁」",
          "会话期间该账号 worker 会让出 profile 锁" not in SERVER)
    check("38 三个弹窗入口都有本机 403",
          SERVER.count('raise HTTPException(403, "本机窗口只能') == 3)

    html = src("frontend/dist/index.html")
    m3 = re.search(r'src="/(assets/[^"]+\.js)"', html)
    jspath = os.path.join(ROOT, "frontend", "dist",
                          m3.group(1).replace("/", os.sep)) if m3 else ""
    check("39 dist/index.html 引用的 JS 存在", bool(m3) and os.path.exists(jspath), jspath)
    bundle = io.open(jspath, encoding="utf-8", errors="replace").read() if jspath else ""
    check("40 新 JS 含大屏串流参数", "view=dashboard" in bundle, len(bundle))
    check("41 新 JS 含新按钮文案", "在网页标签里打开直播大屏" in bundle)
    check("42 新 JS 含大屏串流状态文案", "直播大屏串流中" in bundle)


if __name__ == "__main__":
    asyncio.run(main())
    bad = [n for n, ok, _ in RESULT if not ok]
    print("\n=== 结果: %d/%d 通过 ===" % (len(RESULT) - len(bad), len(RESULT)))
    if bad:
        print("FAILED:", ", ".join(bad))
        sys.exit(1)
    print("ALL PASS")
