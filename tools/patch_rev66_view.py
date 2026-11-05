# -*- coding: utf-8 -*-
"""rev66: 统一「另起浏览器」入口 —— 独立临时 profile + 注入登录态,不停 worker。

背景(2026-11-06 母本实证):
  open_dashboard / open_account_browser 用 acc["profile_dir"] 直接起浏览器,
  纯 HTTP 化后磁盘 cookie 不再可靠 -> 开出未登录页;更严重的是它们调
  stop_account(),清 _run_intent + 写 human_stopped -> 用户点一下「大屏」
  等于手动停号,重启后「无需要自动恢复的账号」,直播数据从此不再更新。
  remote_view 虽已注入登录态(rev37),但 _park_worker 仍停 worker,WS 在 hello
  前断开会留下僵尸会话,账号停到看门狗 5 分钟后才恢复。

修法:照搬 local_window 已验证的模式 —— worker context 现导登录态(优先) /
config.storage_state 兜底 -> 独立临时 profile -> add_cookies + localStorage init
script。三个入口都不再停 worker、不改运行意图,关闭时只清自己的临时目录。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path(rel):
    return os.path.join(ROOT, rel.replace("/", os.sep))


def read(rel):
    with io.open(path(rel), encoding="utf-8") as f:
        return f.read()


def write(rel, s):
    with io.open(path(rel), "w", encoding="utf-8", newline="") as f:
        f.write(s)


def sub1(s, old, new, tag):
    n = s.count(old)
    assert n == 1, "%s: 命中 %d 次(应为 1)" % (tag, n)
    return s.replace(old, new, 1)


def replace_func(s, sig, new, tag):
    """按函数边界整体替换一个方法(从 sig 行到下一个同级 def 之前)。"""
    i = s.find(sig)
    assert i >= 0, "%s: 找不到签名 %s" % (tag, sig)
    m = re.compile(r"\n    (?:async )?def ", re.S).search(s, i + len(sig))
    end = m.start() if m else len(s)
    return s[:i] + new.rstrip() + "\n" + s[end:]


# =====================================================================
# 1) local_window.py:新增三个共享 helper(复用已有的 _local_storage_script / _reset_dir)
# =====================================================================
HELPERS = '''

# ===================== rev66: 另起浏览器统一登录态 =====================
async def export_login_state(manager, account_id, account, tag="view"):
    """worker 还活着时只读导出现役登录态;没跑/导出失败回落 config.storage_state。

    rev66: 纯 HTTP 化之后 profile 磁盘 cookie 不再可靠(cookie 权威副本在浏览器
    内存与 config.accounts[].storage_state),另起的浏览器必须显式注入,否则开出
    的是扫码页。调用方必须在 worker 被停之前调本函数。
    """
    w = (getattr(manager, "workers", {}) or {}).get(account_id)
    ctx = getattr(w, "context", None) if w else None
    if ctx is not None:
        try:
            st = await ctx.storage_state()
            if (st or {}).get("cookies"):
                logger.info(f"[{tag}] {account_id} 从运行中的 context 导出登录态 "
                            f"cookies={len(st.get('cookies') or [])} 条")
                return st
        except Exception as e:
            logger.warning(f"[{tag}] {account_id} 导出登录态失败(回落 config): {e}")
    st = (account or {}).get("storage_state") or {}
    if st.get("cookies"):
        logger.info(f"[{tag}] {account_id} 使用 config 保存的登录态 "
                    f"cookies={len(st.get('cookies') or [])} 条")
        return st
    return None


async def inject_login_state(ctx, state, account_id, tag="view"):
    """cookie + localStorage 一起注入。缺 localStorage 页面照样当未登录(rev37)。"""
    if not state:
        return False
    try:
        ck = state.get("cookies") or []
        if ck:
            await ctx.add_cookies(ck)
        origins = state.get("origins") or []
        if origins:
            await ctx.add_init_script(_local_storage_script(origins))
        logger.info(f"[{tag}] {account_id} 已注入登录态 "
                    f"cookies={len(ck)} origins={len(origins)}")
        return True
    except Exception as e:
        logger.warning(f"[{tag}] {account_id} 注入登录态失败: {e}")
        return False


def new_view_profile(suffix):
    """独立临时 profile;_view_ 前缀由 sweep_stale_view_profiles 启动时清理。"""
    p = os.path.join(".", "profiles", "_view_%s" % suffix)
    _reset_dir(p)
    return p


def drop_view_profile(p):
    """只删自己的临时 profile,绝不碰账号的 profile_dir(worker 还在用它)。"""
    if not p:
        return
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass
'''

s = read("backend/local_window.py")
assert "def export_login_state(" not in s, "helper 已存在,别重复打补丁"
s = s.rstrip() + "\n" + HELPERS
write("backend/local_window.py", s)
print("[1/6] local_window.py: 新增 export/inject/new/drop helper")

# =====================================================================
# 2) account_manager.py:import / 状态字典 / 两个 open / hold / logout / shutdown
# =====================================================================
s = read("backend/account_manager.py")

s = sub1(s,
         "from .browser import launch_stealth, close_context_safely, reap_profile_orphans\n"
         "from .selectors import COMMENT_URL, POST_CREATE_URL",
         "from .browser import launch_stealth, close_context_safely, reap_profile_orphans\n"
         "from .local_window import (export_login_state, inject_login_state,\n"
         "                           new_view_profile, drop_view_profile)\n"
         "from .selectors import COMMENT_URL, POST_CREATE_URL",
         "am/import")

s = sub1(s,
         "        self._open_browsers = {}         # account_id -> headed BrowserContext(用户手动操作)",
         "        self._open_browsers = {}         # account_id -> headed BrowserContext(用户手动操作)\n"
         "        self._open_browser_profiles = {}  # rev66: account_id -> 独立临时 profile 路径",
         "am/init")

s = sub1(s,
         "        ctx = self._open_browsers.pop(account_id, None)\n"
         "        if ctx:\n"
         "            try:\n"
         "                await close_context_safely(ctx, acc.get(\"profile_dir\"), f\"[{account_id}] logout\")",
         "        ctx = self._open_browsers.pop(account_id, None)\n"
         "        temp = self._open_browser_profiles.pop(account_id, None)\n"
         "        if ctx:\n"
         "            try:\n"
         "                await close_context_safely(ctx, temp, f\"[{account_id}] logout\")",
         "am/logout")

NEW_OPEN_BROWSER = '''    async def open_account_browser(self, account_id):
        """rev66: 独立临时 profile + 注入登录态,不停 worker、不动运行意图。

        旧实现用 acc["profile_dir"] 起浏览器并先调 stop_account():后者会清
        _run_intent + 写 human_stopped,用户点一下「打开」就等于手动停号,
        重启后不再自动恢复(2026-11-06 母本实证)。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        if account_id in self._open_browsers:
            return True
        state = await export_login_state(self, account_id, acc, "open-browser")
        if not state:
            logger.warning(f"[{account_id}] 没有可用登录态,打不开浏览器,请先扫码登录")
            return False
        pw = await self._ensure_playwright()
        temp = new_view_profile("%s_headed" % account_id)
        ctx = None
        try:
            ctx = await launch_stealth(pw, temp, headless=False, window_size=(1920, 1035))
            await inject_login_state(ctx, state, account_id, "open-browser")
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(POST_CREATE_URL, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"[{account_id}] 打开浏览器 goto 失败(忽略): {e}")
            self._open_browsers[account_id] = ctx
            self._open_browser_profiles[account_id] = temp
            asyncio.create_task(self._hold_open_browser(account_id))
            logger.info(f"[{account_id}] 已打开 headed 浏览器(独立 profile,后台采集不受影响)")
            return True
        except Exception as e:
            logger.error(f"[{account_id}] 打开浏览器失败: {e}")
            if ctx is not None:
                try:
                    await close_context_safely(ctx, temp, f"[{account_id}] open-browser")
                except Exception:
                    pass
            drop_view_profile(temp)
            return False
'''
s = replace_func(s, "    async def open_account_browser(self, account_id):",
                 NEW_OPEN_BROWSER, "am/open_account_browser")

NEW_OPEN_DASHBOARD = '''    async def open_dashboard(self, account_id):
        """rev66: 打开当前直播的 dashboardV4 大屏,不停 worker、不动运行意图。

        live_object_id 仍从运行中的 worker.live_fetcher 取;登录态从 worker
        context 现导 / config.storage_state 兜底,注入到独立临时 profile。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False, "账号不存在"
        worker = self.workers.get(account_id)
        live_object_id = worker.live_fetcher.live_object_id if worker and getattr(worker, "live_fetcher", None) else None
        if not live_object_id:
            return False, "账号未在直播或未拿到 liveObjectId"
        if account_id in self._open_browsers:
            return True, ""
        state = await export_login_state(self, account_id, acc, "dashboard")
        if not state:
            return False, "该账号没有可用登录态,请先扫码登录"
        pw = await self._ensure_playwright()
        temp = new_view_profile("%s_headed" % account_id)
        ctx = None
        try:
            ctx = await launch_stealth(pw, temp, headless=False, window_size=(1920, 1035))
            await inject_login_state(ctx, state, account_id, "dashboard")
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            url = f"https://channels.weixin.qq.com/platform/statistic/dashboardV4?objetctId={live_object_id}&entrance_id=2"
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"[{account_id}] 打开 dashboardV4 goto 失败(忽略): {e}")
            self._open_browsers[account_id] = ctx
            self._open_browser_profiles[account_id] = temp
            asyncio.create_task(self._hold_open_browser(account_id))
            logger.info(f"[{account_id}] 已打开 dashboardV4 大屏 liveObjectId={live_object_id}(独立 profile,后台采集不受影响)")
            return True, ""
        except Exception as e:
            logger.error(f"[{account_id}] 打开 dashboardV4 失败: {e}")
            if ctx is not None:
                try:
                    await close_context_safely(ctx, temp, f"[{account_id}] dashboard")
                except Exception:
                    pass
            drop_view_profile(temp)
            return False, str(e)
'''
s = replace_func(s, "    async def open_dashboard(self, account_id):",
                 NEW_OPEN_DASHBOARD, "am/open_dashboard")

NEW_HOLD = '''    async def _hold_open_browser(self, account_id, was_running=None):
        """rev66: 只收尾自己的独立 profile。worker 从未停过,不需要恢复。

        close_context_safely 必须传临时 profile:传账号 profile_dir 会在 close
        异常时按 --user-data-dir 兜底 kill,把正在跑的 worker 浏览器一起杀掉。
        """
        ctx = self._open_browsers.get(account_id)
        temp = self._open_browser_profiles.get(account_id)
        if not ctx:
            return
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    if not ctx.pages:
                        break
                except Exception:
                    break
        except Exception:
            pass
        await close_context_safely(ctx, temp, f"[{account_id}] headed")
        self._open_browsers.pop(account_id, None)
        self._open_browser_profiles.pop(account_id, None)
        drop_view_profile(temp)
        logger.info(f"[{account_id}] headed 浏览器已关闭(后台采集未中断)")
'''
s = replace_func(s, "    async def _hold_open_browser(self, account_id, was_running=True):",
                 NEW_HOLD, "am/_hold_open_browser")

s = sub1(s,
         "        for acc_id, ctx in list(self._open_browsers.items()):\n"
         "            acc = next((a for a in self.config.get(\"accounts\", []) if a[\"id\"] == acc_id), None)\n"
         "            await close_context_safely(ctx, acc[\"profile_dir\"] if acc else None,\n"
         "                                       f\"[{acc_id}] headed\")\n"
         "        self._open_browsers.clear()",
         "        for acc_id, ctx in list(self._open_browsers.items()):\n"
         "            temp = self._open_browser_profiles.get(acc_id)\n"
         "            await close_context_safely(ctx, temp, f\"[{acc_id}] headed\")\n"
         "            drop_view_profile(temp)\n"
         "        self._open_browsers.clear()\n"
         "        self._open_browser_profiles.clear()",
         "am/shutdown")

write("backend/account_manager.py", s)
print("[2/6] account_manager.py: 两个 open / hold / logout / shutdown 全部改为独立 profile")

# =====================================================================
# 3) remote_view.py:不再 park worker、独立 profile、明确无登录态错误
# =====================================================================
s = read("backend/remote_view.py")

s = sub1(s,
         "from .local_window import _local_storage_script",
         "from .local_window import (export_login_state, inject_login_state,\n"
         "                           new_view_profile, drop_view_profile,\n"
         "                           _local_storage_script)",
         "rv/import")

s = sub1(s,
         "同一个 profile_dir 不能同时被两处占用(Chromium profile 锁),所以远程会话期间必须\n"
         "先停 worker。但**不能走 stop_account**:它会写 human_stopped、清运行意图,\n"
         "把「临时让位」当成「人工停止」,之后自动发作品就不会再拉起这个号。\n"
         "这里用 _park_worker 只摘 workers 里的 worker,两样都不动;关闭时按原样恢复。",
         "rev66: 改用独立临时 profile + 注入登录态,**不停 worker、不动运行意图**。\n"
         "旧实现共用一个 profile_dir,必须先 _park_worker 让锁;WS 在 hello 之前断开\n"
         "会留下僵尸会话,账号一直停到看门狗 5 分钟后才恢复。现在三条「另起浏览器」\n"
         "入口(本机窗口 / 大屏 / 远程串流)各用各的临时 profile,可并存且不影响采集。",
         "rv/doc")

s = sub1(s,
         "async def _park_worker(manager, account_id):\n"
         "    \"\"\"临时摘掉 worker 让出 profile 锁,不改人工停止标记/运行意图。\"\"\"\n"
         "    w = (getattr(manager, \"workers\", {}) or {}).pop(account_id, None)\n"
         "    if w is not None:\n"
         "        try:\n"
         "            await w.stop()\n"
         "        except Exception as e:\n"
         "            logger.warning(f\"[remote] 停 worker 失败 {account_id}: {e}\")\n"
         "    return w\n\n\n",
         "",
         "rv/park")

s = sub1(s,
         "        self._was_running = self.account_id in (getattr(self.manager, \"workers\", {}) or {})\n"
         "        self._was_human_stopped = self.account_id in getattr(\n"
         "            self.manager, \"human_stopped\", set())\n\n"
         "        # rev37: 必须在 park 之前把登录态导出来 —— park 会把 worker 的 context\n"
         "        # 一起关掉,之后就再也拿不到了。而 profile 磁盘的 cookie 是不可靠的\n"
         "        # (Chromium 延迟写盘,进程被中途关掉就是 0 条,见 rev29),只靠磁盘会出现\n"
         "        # 「worker 明明登录着,远程窗口一打开却是扫码页」(2026-10-21 用户实证)。\n"
         "        state = None\n"
         "        try:\n"
         "            _w = (getattr(self.manager, \"workers\", {}) or {}).get(self.account_id)\n"
         "            _ctx = getattr(_w, \"context\", None) if _w else None\n"
         "            if _ctx is not None:\n"
         "                state = await _ctx.storage_state()\n"
         "                logger.info(f\"[remote] {self.account_id} 从运行中的 context 导出\"\n"
         "                            f\"登录态 cookies={len(state.get('cookies') or [])} 条\")\n"
         "        except Exception as e:\n"
         "            logger.warning(f\"[remote] {self.account_id} 导出登录态失败: {e}\")\n"
         "            state = None\n"
         "        if not (state or {}).get(\"cookies\"):\n"
         "            # worker 没跑 / 导出失败 -> 退回 config 里存的扫码登录态\n"
         "            _ss = acc.get(\"storage_state\") or {}\n"
         "            if _ss.get(\"cookies\"):\n"
         "                state = _ss\n"
         "                logger.info(f\"[remote] {self.account_id} 使用 config 内保存的登录态 \"\n"
         "                            f\"cookies={len(_ss.get('cookies') or [])} 条\")\n\n"
         "        await _park_worker(self.manager, self.account_id)\n\n"
         "        pw = await self.manager._ensure_playwright()\n"
         "        self.ctx = await launch_stealth(pw, acc[\"profile_dir\"], headless=True)\n"
         "        pages = list(self.ctx.pages)\n"
         "        self.page = pages[0] if pages else await self.ctx.new_page()\n"
         "        # rev37: 注入登录态(cookie + localStorage)。视频号的身份 aid 存在\n"
         "        # localStorage,只注 cookie 不够 —— 缺了它页面照样把你当没登录。\n"
         "        if state:\n"
         "            try:\n"
         "                _ck = state.get(\"cookies\") or []\n"
         "                if _ck:\n"
         "                    await self.ctx.add_cookies(_ck)\n"
         "                _origins = state.get(\"origins\") or []\n"
         "                if _origins:\n"
         "                    await self.ctx.add_init_script(_local_storage_script(_origins))\n"
         "                logger.info(f\"[remote] {self.account_id} 已注入登录态 \"\n"
         "                            f\"cookies={len(_ck)} origins={len(_origins)}\")\n"
         "            except Exception as e:\n"
         "                logger.warning(f\"[remote] {self.account_id} 注入登录态失败: {e}\")",
         "        # rev66: worker 一直活着,登录态优先从它的 context 现导(最权威),\n"
         "        # 纯 HTTP 账号没有 context 时回落 config.storage_state。\n"
         "        state = await export_login_state(self.manager, self.account_id, acc, \"remote\")\n"
         "        if not state:\n"
         "            raise RuntimeError(\"该账号没有可用登录态,请先扫码登录\")\n\n"
         "        pw = await self.manager._ensure_playwright()\n"
         "        self.profile_dir = new_view_profile(\"%s_remote\" % self.account_id)\n"
         "        self.ctx = await launch_stealth(pw, self.profile_dir, headless=True)\n"
         "        pages = list(self.ctx.pages)\n"
         "        self.page = pages[0] if pages else await self.ctx.new_page()\n"
         "        # 视频号身份 aid 存在 localStorage,只注 cookie 不够(rev37)。\n"
         "        await inject_login_state(self.ctx, state, self.account_id, \"remote\")",
         "rv/open")

s = sub1(s,
         "        self.ctx = None\n"
         "        self.page = None\n"
         "        self.cdp = None",
         "        self.ctx = None\n"
         "        self.page = None\n"
         "        self.cdp = None\n"
         "        self.profile_dir = None     # rev66: 独立临时 profile",
         "rv/init")

s = sub1(s,
         "        if self.ctx is not None:\n"
         "            try:\n"
         "                await close_context_safely(self.ctx, self.acc and self.acc.get(\"profile_dir\"),\n"
         "                                          f\"remote-{self.account_id}\")\n"
         "            except Exception as e:\n"
         "                logger.warning(f\"[remote] 关 context 异常: {e}\")\n"
         "            self.ctx = None\n"
         "        # 恢复原本在跑的账号(原本就是人工停的 -> 保持停)\n"
         "        if self._was_running:\n"
         "            try:\n"
         "                w = await self.manager.start_account(self.account_id)\n"
         "                if w is not None and self._was_human_stopped:\n"
         "                    self.manager.human_stopped.add(self.account_id)\n"
         "                logger.info(f\"[remote] 已恢复抓取 {self.account_id}\")\n"
         "            except Exception as e:\n"
         "                logger.warning(f\"[remote] 恢复抓取失败 {self.account_id}: {e}\")",
         "        if self.ctx is not None:\n"
         "            try:\n"
         "                await close_context_safely(self.ctx, self.profile_dir,\n"
         "                                          f\"remote-{self.account_id}\")\n"
         "            except Exception as e:\n"
         "                logger.warning(f\"[remote] 关 context 异常: {e}\")\n"
         "            self.ctx = None\n"
         "        drop_view_profile(self.profile_dir)\n"
         "        self.profile_dir = None\n"
         "        # rev66: worker 从未停过,不需要(也不能)在这里 start_account ——\n"
         "        # 那会重新写运行意图,把用户手动停的号又拉起来。",
         "rv/close")

s = sub1(s,
         "        s = self.get(account_id)\n"
         "        if s is not None:\n"
         "            await s.refresh_url()\n"
         "            return s, True",
         "        s = self.get(account_id)\n"
         "        if s is not None:\n"
         "            # rev66: 页面已关 / CDP 掉了的「僵尸会话」不能复用,否则前端\n"
         "            # 连上就报「建立会话失败」,而且这个会话永远没人收尾。\n"
         "            try:\n"
         "                if (s.page is not None and not s.page.is_closed()\n"
         "                        and s.cdp is not None and s.ctx is not None):\n"
         "                    await s.refresh_url()\n"
         "                    return s, True\n"
         "            except Exception:\n"
         "                pass\n"
         "            logger.info(f\"[remote] 复用会话已失效,重建 {account_id}\")\n"
         "            await self.close(account_id, \"stale\")",
         "rv/registry")

write("backend/remote_view.py", s)
print("[3/6] remote_view.py: 独立 profile / 不停 worker / 僵尸会话不复用")

# =====================================================================
# 4) server.py:WS 建立失败时清理已登记的会话
# =====================================================================
s = read("backend/server.py")
s = sub1(s,
         "    except Exception as e:\n"
         "        logger.warning(f\"[remote] 建立会话失败 {account}: {e}\")\n"
         "        try:\n"
         "            await ws.send_json({\"t\": \"error\",\n"
         "                                \"message\": f\"{type(e).__name__}: {str(e)[:200]}\"})\n"
         "            await ws.close()\n"
         "        except Exception:\n"
         "            pass\n"
         "        return",
         "    except Exception as e:\n"
         "        logger.warning(f\"[remote] 建立会话失败 {account}: {e}\")\n"
         "        # rev66: open() 可能已经把会话登记进 registry(日志里出现过\n"
         "        # 「会话已开启」紧接「建立会话失败」),不清理会留下僵尸会话。\n"
         "        try:\n"
         "            await remote_registry.close(account, \"open_failed\")\n"
         "        except Exception:\n"
         "            pass\n"
         "        try:\n"
         "            await ws.send_json({\"t\": \"error\",\n"
         "                                \"message\": f\"{type(e).__name__}: {str(e)[:200]}\"})\n"
         "            await ws.close()\n"
         "        except Exception:\n"
         "            pass\n"
         "        return",
         "srv/remote_ws")
write("backend/server.py", s)
print("[4/6] server.py: WS 建立失败清理会话")

# =====================================================================
# 5) 前端文案
# =====================================================================
s = read("frontend/src/components/posts/PlayDialog.tsx")
s = sub1(s,
         " * 地址带时效(视频号 CDN 会过期),所以每次打开都问后端要一次;\n"
         " * 后端发现过期会用该视频号的浏览器现取,拿不到就明确提示原因。",
         " * 地址带时效(视频号 CDN 会过期),每次打开都由后端用当前账号的登录态\n"
         " * 实时取最新地址(纯 HTTP 直连),拿不到就明确提示原因。",
         "ui/play-doc")
s = sub1(s,
         "                  {data.refreshed ? \"播放地址已过期,已用当前登录态现取\" : \"播放地址有效\"}",
         "                  已实时获取播放地址",
         "ui/play-text")
write("frontend/src/components/posts/PlayDialog.tsx", s)

s = read("frontend/src/pages/LiveScreenPage.tsx")
s = sub1(s,
         '      toast.success("已打开直播大屏,关闭后自动恢复")',
         '      toast.success("已打开直播大屏(不影响后台采集)")',
         "ui/dashboard-toast")
write("frontend/src/pages/LiveScreenPage.tsx", s)

s = read("frontend/src/pages/RemotePage.tsx")
s = sub1(s,
         " * 5 分钟没有任何操作自动断开并恢复数据抓取 —— 串流期间该账号的 worker 是停的。",
         " * 5 分钟没有任何操作自动断开。串流用独立浏览器 + 注入登录态,后台采集不中断。",
         "ui/remote-doc")
assert s.count('"5 分钟没有操作,已自动断开并恢复数据抓取"') == 2, "remote 文案命中数不对"
s = s.replace('"5 分钟没有操作,已自动断开并恢复数据抓取"',
              '"5 分钟没有操作,已自动断开"')
write("frontend/src/pages/RemotePage.tsx", s)
print("[5/6] 前端文案:PlayDialog / LiveScreenPage / RemotePage")

# =====================================================================
# 6) 自校验
# =====================================================================
checks = [
    ("backend/local_window.py", "async def export_login_state(", True),
    ("backend/local_window.py", "async def inject_login_state(", True),
    ("backend/local_window.py", "def new_view_profile(", True),
    ("backend/local_window.py", "def drop_view_profile(", True),
    ("backend/account_manager.py", "from .local_window import (export_login_state", True),
    ("backend/account_manager.py", "self._open_browser_profiles = {}", True),
    ("backend/account_manager.py", "await self.stop_account(account_id)\n        pw = await self._ensure_playwright()", False),
    ("backend/account_manager.py", "new_view_profile(\"%s_headed\" % account_id)", True),
    ("backend/account_manager.py", "close_context_safely(ctx, temp,", True),
    ("backend/account_manager.py", "worker 从未停过,不需要恢复", True),
    ("backend/remote_view.py", "async def _park_worker(", False),
    ("backend/remote_view.py", "await _park_worker(", False),
    ("backend/remote_view.py", "new_view_profile(\"%s_remote\" % self.account_id)", True),
    ("backend/remote_view.py", "raise RuntimeError(\"该账号没有可用登录态,请先扫码登录\")", True),
    ("backend/remote_view.py", "复用会话已失效,重建", True),
    ("backend/remote_view.py", "await self.manager.start_account(self.account_id)", False),
    ("backend/server.py", 'await remote_registry.close(account, "open_failed")', True),
    ("frontend/src/components/posts/PlayDialog.tsx", "已实时获取播放地址", True),
    ("frontend/src/components/posts/PlayDialog.tsx", "播放地址已过期", False),
    ("frontend/src/pages/LiveScreenPage.tsx", "不影响后台采集", True),
    ("frontend/src/pages/RemotePage.tsx", "后台采集不中断", True),
    ("frontend/src/pages/RemotePage.tsx", "恢复数据抓取", False),
]
bad = 0
for rel, needle, want in checks:
    got = needle in read(rel)
    if got != want:
        bad += 1
        print("  FAIL", rel, repr(needle[:48]), "want=", want, "got=", got)
assert bad == 0, "自校验 %d 项失败" % bad
print("[6/6] SELFCHECK PASS (%d 项)" % len(checks))
