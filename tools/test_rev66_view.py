# -*- coding: utf-8 -*-
"""rev66 单测：另起浏览器统一为「独立临时 profile + 注入登录态 + 不停 worker」。

本机没装 playwright,account_manager 用 AST 抽出三个方法单独 exec(受控 namespace
里故意不提供 stop_account/start_account,代码要是还碰就 AttributeError = 强断言)。
remote_view / local_window 不依赖 playwright,直接 import 真模块跑。
"""
import ast
import asyncio
import io
import os
import sys
from types import SimpleNamespace

ROOT = r"D:\SPHGJ"
sys.path.insert(0, ROOT)
from backend import local_window as lw      # noqa: E402
from backend import remote_view as rv      # noqa: E402

RESULT = []


def check(name, cond, extra=""):
    RESULT.append((name, bool(cond), extra))
    print("  %s %s%s" % ("OK " if cond else "BAD", name,
                         "" if cond else "  <- " + str(extra)[:170]))


def src(rel):
    with io.open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


STATE = {
    "cookies": [{"name": "sessionid", "value": "v%d" % i,
                 "domain": "channels.weixin.qq.com"} for i in range(5)],
    "origins": [{"origin": "https://channels.weixin.qq.com",
                 "localStorage": [{"name": "__ml::aid", "value": "aid-test"}]}],
}
ACC = {"id": "test_account", "name": "测试号",
       "profile_dir": "./profiles/test_account_real",
       "storage_state": STATE, "_log_finder_id": "fid@test"}


class FakePage:
    def __init__(self, closed=False):
        self.url = "https://channels.weixin.qq.com/platform/post/create"
        self._closed = closed
        self.goto_calls = []
        self.handlers = []

    def set_default_timeout(self, n):
        pass

    async def goto(self, url, **kw):
        self.goto_calls.append(url)
        self.url = url
        return None

    def on(self, ev, cb):
        self.handlers.append(ev)

    def is_closed(self):
        return self._closed


class FakeCDP:
    def __init__(self):
        self.sends = []
        self.handlers = []

    async def send(self, method, params=None):
        self.sends.append(method)
        return {}

    def on(self, ev, cb):
        self.handlers.append(ev)


class FakeCtx:
    def __init__(self, state=STATE, fail_export=False):
        self._state = state
        self._fail = fail_export
        self.pages = []
        self.cookies_added = []
        self.scripts = []
        self.closed = False
        self.cdp = FakeCDP()

    async def storage_state(self):
        if self._fail:
            raise RuntimeError("context closed")
        return self._state

    async def add_cookies(self, ck):
        self.cookies_added.append(ck)

    async def add_init_script(self, s):
        self.scripts.append(s)

    async def new_page(self):
        p = FakePage()
        self.pages.append(p)
        return p

    async def new_cdp_session(self, page):
        return self.cdp

    async def close(self):
        self.closed = True


def fake_manager(ctx="missing"):
    """ctx='missing' 模拟纯 HTTP 账号(没有浏览器 context)。"""
    w = SimpleNamespace(context=None if ctx == "missing" else ctx,
                        live_fetcher=SimpleNamespace(live_object_id="OBJ-123"))
    return SimpleNamespace(
        workers={"test_account": w},
        human_stopped=set(),
        _ensure_playwright=lambda: _coro(object()),
        start_account=None,      # 一旦被调用就是 TypeError -> 强断言
    )


async def _coro(v):
    return v


# =====================================================================
# A. local_window 新增 helper
# =====================================================================
async def test_helpers():
    print("\n--- A. 登录态导出/注入 helper ---")
    m = fake_manager(FakeCtx())
    st = await lw.export_login_state(m, "test_account", ACC, "test")
    check("1 context 优先,拿到 5 条 cookie", st is STATE and len(st["cookies"]) == 5, st)

    m2 = fake_manager(FakeCtx(fail_export=True))
    st2 = await lw.export_login_state(m2, "test_account", ACC, "test")
    check("2 context 导出异常回落 config", st2 is STATE, st2)

    m3 = fake_manager("missing")
    st3 = await lw.export_login_state(m3, "test_account", ACC, "test")
    check("3 纯 HTTP 无 context 回落 config", st3 is STATE, st3)

    st4 = await lw.export_login_state(fake_manager("missing"), "test_account",
                                      {"id": "x"}, "test")
    check("4 两处都没有登录态 -> None", st4 is None, st4)

    c = FakeCtx()
    ok = await lw.inject_login_state(c, STATE, "test_account", "test")
    check("5 注入 cookie 5 条", ok and len(c.cookies_added) == 1
          and len(c.cookies_added[0]) == 5, c.cookies_added)
    check("6 注入 localStorage init script", len(c.scripts) == 1
          and "localStorage.setItem" in c.scripts[0], c.scripts)
    check("7 state 为空不注入", not await lw.inject_login_state(c, None, "x", "test"))

    # 导出时绝不碰 worker 状态
    check("8 导出不 pop workers / 不调 stop", len(m.workers) == 1, m.workers)


# =====================================================================
# B. remote_view:独立 profile、不停 worker、僵尸会话
# =====================================================================
async def test_remote():
    print("\n--- B. remote_view ---")
    orig = (rv.launch_stealth, rv.close_context_safely, rv.new_view_profile,
            rv.drop_view_profile, rv.export_login_state, rv.inject_login_state)
    calls = {"launch": [], "close": [], "drop": []}
    ctx = FakeCtx()

    async def fake_launch(pw, profile, headless=True, **kw):
        calls["launch"].append((profile, headless))
        return ctx

    async def fake_close(c, profile_dir=None, log_tag=""):
        calls["close"].append(profile_dir)

    rv.launch_stealth = fake_launch
    rv.close_context_safely = fake_close
    rv.new_view_profile = lambda suffix: "./profiles/_view_%s" % suffix
    rv.drop_view_profile = lambda p: calls["drop"].append(p)
    rv.export_login_state = lw.export_login_state
    rv.inject_login_state = lw.inject_login_state
    try:
        m = fake_manager(FakeCtx())
        sess = rv.RemoteSession(m, "test_account",
                                {"accounts": [ACC]}, None)
        meta = await sess.open()
        check("9 会话能建立(hello)", meta.get("t") == "hello", meta)
        check("10 不停 worker", len(m.workers) == 1
              and "test_account" in m.workers, m.workers)
        check("11 用独立临时 profile,不占账号 profile",
              calls["launch"][0][0].startswith("./profiles/_view_")
              and calls["launch"][0][0] != ACC["profile_dir"], calls["launch"])
        check("12 登录态已注入(cookie+localStorage)",
              len(ctx.cookies_added) == 1 and len(ctx.scripts) == 1,
              (ctx.cookies_added, ctx.scripts))
        check("13 goto 目标页", ctx.pages and ctx.pages[0].goto_calls, ctx.pages)
        await sess.close("test")
        check("14 关闭传临时 profile(不误杀 worker)",
              calls["close"] == ["./profiles/_view_test_account_remote"], calls["close"])
        check("15 关闭清理临时目录", len(calls["drop"]) == 1, calls["drop"])
        check("16 关闭不调 start_account", m.start_account is None)

        # 无登录态 -> 明确报错,不再开扫码页
        m2 = fake_manager("missing")
        sess2 = rv.RemoteSession(m2, "test_account", {"accounts": [{"id": "test_account"}]}, None)
        try:
            await sess2.open()
            check("17 无登录态抛明确错误", False, "竟然没抛")
        except RuntimeError as e:
            check("17 无登录态抛明确错误", "请先扫码登录" in str(e), str(e))

        # 僵尸会话不复用
        reg = rv.RemoteRegistry(m, {"accounts": [ACC]}, None)
        stale = rv.RemoteSession(m, "test_account", {"accounts": [ACC]}, None)
        stale.closed = False
        stale.page = FakePage(closed=True)
        stale.cdp = FakeCDP()
        stale.ctx = FakeCtx()
        reg.sessions["test_account"] = stale
        new_sess, reused = await reg.open("test_account")
        check("18 page 已关的僵尸会话不复用", reused is False
              and new_sess is not stale, (reused, new_sess is stale))
        await new_sess.close("test")
        await reg.close_all()
    finally:
        (rv.launch_stealth, rv.close_context_safely, rv.new_view_profile,
         rv.drop_view_profile, rv.export_login_state, rv.inject_login_state) = orig


# =====================================================================
# C. account_manager 三个方法(AST 抽取,受控 namespace)
# =====================================================================
def extract(rel, name, ns):
    """把方法单独 exec 进调用方给的 globals(必须是同一个 dict,否则函数
    的 __globals__ 会指向另一个空 dict,注入的 fake 全都看不见)。"""
    tree = ast.parse(src(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, "<%s>" % name, "exec"), ns)
            return ns[name]
    raise AssertionError("找不到 %s" % name)


async def test_manager():
    print("\n--- C. account_manager 大屏 / 打开浏览器 / 收尾 ---")
    am = src("backend/account_manager.py")
    calls = {"launch": [], "close": [], "drop": [], "hold": []}
    ctxs = []

    async def fake_launch(pw, profile, headless=True, **kw):
        calls["launch"].append(profile)
        c = FakeCtx()
        ctxs.append(c)
        return c

    async def fake_close(c, profile_dir=None, log_tag=""):
        calls["close"].append(profile_dir)

    ns = {
        "asyncio": asyncio,
        "logger": SimpleNamespace(info=lambda *a, **k: None,
                                  warning=lambda *a, **k: None,
                                  error=lambda *a, **k: None,
                                  debug=lambda *a, **k: None),
        "POST_CREATE_URL": "https://channels.weixin.qq.com/platform/post/create",
        "launch_stealth": fake_launch,
        "close_context_safely": fake_close,
        "export_login_state": lw.export_login_state,
        "inject_login_state": lw.inject_login_state,
        "new_view_profile": lambda suffix: "./profiles/_view_%s" % suffix,
        "drop_view_profile": lambda p: calls["drop"].append(p),
    }

    class FakeSelf:
        def __init__(self, has_state=True):
            a = dict(ACC)
            if not has_state:
                a.pop("storage_state", None)
            self.config = {"accounts": [a]}
            self.workers = {"test_account": SimpleNamespace(
                # 无登录态场景同时模拟纯 HTTP 账号(没有 context),否则会从
                # worker context 现导到登录态,那本来就该返回成功。
                context=None if not has_state else FakeCtx(),
                live_fetcher=SimpleNamespace(live_object_id="OBJ-123"))}
            self._open_browsers = {}
            self._open_browser_profiles = {}
            # 故意不提供 stop_account / start_account:旧实现会 AttributeError

        async def _ensure_playwright(self):
            return object()

        async def _hold_open_browser(self, account_id, was_running=None):
            calls["hold"].append(account_id)

    for name in ("open_dashboard", "open_account_browser", "_hold_open_browser"):
        ns[name] = extract("backend/account_manager.py", name, ns)

    self_ = FakeSelf()
    ok, msg = await ns["open_dashboard"](self_, "test_account")
    check("19 大屏返回成功", ok is True and msg == "", (ok, msg))
    check("20 不停 worker(workers 仍在 + 无 stop_account)",
          len(self_.workers) == 1 and "test_account" in self_.workers)
    check("21 大屏用独立临时 profile",
          calls["launch"][0] == "./profiles/_view_test_account_headed", calls["launch"])
    check("22 大屏 URL 带 liveObjectId",
          ctxs[0].pages and "OBJ-123" in ctxs[0].pages[0].goto_calls[0],
          ctxs[0].pages[0].goto_calls if ctxs[0].pages else None)
    check("23 大屏注入了登录态",
          len(ctxs[0].cookies_added) == 1 and len(ctxs[0].scripts) == 1)
    check("24 临时 profile 已登记", self_._open_browser_profiles.get("test_account")
          == calls["launch"][0], self_._open_browser_profiles)

    self2 = FakeSelf(has_state=False)
    ok2, msg2 = await ns["open_dashboard"](self2, "test_account")
    check("25 无登录态给明确原因而不是开扫码页",
          ok2 is False and "扫码" in msg2, (ok2, msg2))

    self3 = FakeSelf()
    ok3 = await ns["open_account_browser"](self3, "test_account")
    check("26 open-browser 同样不停 worker", ok3 is True and len(self3.workers) == 1)

    # _hold_open_browser:传临时 profile 且不重启 worker
    self4 = FakeSelf()
    c = FakeCtx()
    self4._open_browsers["test_account"] = c
    self4._open_browser_profiles["test_account"] = "./profiles/_view_test_account_headed"
    await ns["_hold_open_browser"](self4, "test_account")
    check("27 收尾 close_context_safely 传临时 profile",
          calls["close"][-1] == "./profiles/_view_test_account_headed", calls["close"])
    check("28 收尾清理临时目录", len(calls["drop"]) >= 1, calls["drop"])
    check("29 收尾不再 start_account / 不动 workers", len(self4.workers) == 1)
    check("30 _open_browsers 已清空", not self4._open_browsers
          and not self4._open_browser_profiles)

    check("31 源码里两个 open 已无 stop_account 调用",
          "await self.stop_account(account_id)\n        pw" not in am)
    check("32 源码里 hold 已无 start_account",
          "self.start_account(account_id)" not in am.split("async def _hold_open_browser")[1][:1600])


# =====================================================================
# D. server.py / 前端 / dist 静态断言
# =====================================================================
def test_static():
    print("\n--- D. WS 异常清理与前端文案 ---")
    sv = src("backend/server.py")
    check("33 WS 建立失败清理会话",
          'await remote_registry.close(account, "open_failed")' in sv)
    pd = src("frontend/src/components/posts/PlayDialog.tsx")
    check("34 播放文案改成「已实时获取播放地址」", "已实时获取播放地址" in pd)
    check("35 旧的「地址已过期」文案已删", "播放地址已过期" not in pd)
    ls = src("frontend/src/pages/LiveScreenPage.tsx")
    check("36 大屏 toast 不再说「关闭后自动恢复」", "不影响后台采集" in ls)
    rp = src("frontend/src/pages/RemotePage.tsx")
    check("37 远程页说明改成不中断采集", "后台采集不中断" in rp)
    check("38 远程页不再说「恢复数据抓取」", "恢复数据抓取" not in rp)
    dist = os.path.join(ROOT, "frontend", "dist")
    js = ""
    for f in os.listdir(os.path.join(dist, "assets")) if os.path.isdir(os.path.join(dist, "assets")) else []:
        if f.endswith(".js"):
            js += io.open(os.path.join(dist, "assets", f), encoding="utf-8", errors="replace").read()
    check("39 dist 已重新构建且含新文案", "已实时获取播放地址" in js, len(js))
    check("40 dist 不含旧文案", "播放地址已过期" not in js)


async def main():
    await test_helpers()
    await test_remote()
    await test_manager()
    test_static()
    bad = [r for r in RESULT if not r[1]]
    print("\n=== %d/%d 通过 ===" % (len(RESULT) - len(bad), len(RESULT)))
    if bad:
        for r in bad:
            print("  FAILED:", r[0], str(r[2])[:120])
        raise SystemExit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
