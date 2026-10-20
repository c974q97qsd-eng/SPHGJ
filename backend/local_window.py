# -*- coding: utf-8 -*-
"""本机可见浏览器窗口(rev31)。

需求
----
账号卡片「打开」= 在**运行程序的本机**直接拉起一个真实 Chromium 窗口，
并且**不影响后台数据抓取(含直播大屏的数据流)**。

硬约束与绕法
------------
同一个 profile 同一时刻只能有一个 Chromium 实例(profile 锁)。后台抓取的 worker
和直播大屏(live_page)用的是同一个 context,所以绝不能复用它的 profile。分两种情形:

1. **该账号 worker 正在跑(context 存活)**
   从它的 context 调用 `storage_state()` 导出登录态 —— 这是**只读**操作,不影响
   正在进行的抓取/直播;然后用一个**临时 profile** + 注入登录态另起一个独立窗口。
   两个浏览器各用各的 profile,互不干扰。

2. **worker 没跑(profile 空着)**
   直接用账号自己的 profile 起窗口,登录态天然完整,不需要注入。

localStorage 的处理
-------------------
`storage_state` 里除了 cookies 还有每个 origin 的 localStorage;视频号的登录身份
(`__ml::aid` / `finder_username`)就存在 localStorage 里,只注入 cookie 不够。
这里用 `add_init_script` 在每次导航前按 origin 回填,保证任何页面加载时都带着。

空闲关闭
--------
真实窗口的鼠标键盘是操作系统事件,Playwright 拿不到。改用页面内 JS 记录最后活动
时间(节流到 1 秒一次),后端每 15 秒读一次,超过 5 分钟无活动就关窗口 + 清临时
profile,避免一直占着内存。
"""
import asyncio
import json
import logging
import os
import shutil
import time

from .browser import launch_stealth, close_context_safely

logger = logging.getLogger("sphgj")

IDLE_TIMEOUT_SEC = 300          # 5 分钟无操作自动关
IDLE_CHECK_SEC = 15             # 看门狗轮询间隔
MAX_WINDOWS = 3                 # 同时最多 3 个本机窗口(每个约 200~400MB)
WINDOW_W, WINDOW_H = 1500, 950

COMMENT_URL = "https://channels.weixin.qq.com/platform/comment"

# 页面内活动打点:节流 1 秒,只记"用户确实动过"的事件
ACTIVITY_SCRIPT = """
(() => {
  if (window.__sphgjActInstalled) return;
  window.__sphgjActInstalled = true;
  window.__sphgjLastAct = Date.now();
  let last = 0;
  const touch = () => {
    const n = Date.now();
    if (n - last > 1000) { last = n; window.__sphgjLastAct = n; }
  };
  ['mousedown', 'mousemove', 'keydown', 'wheel', 'scroll', 'touchstart'].forEach(
    ev => window.addEventListener(ev, touch, true));
})();
"""


def _local_storage_script(origins):
    """按 origin 回填 localStorage 的 init script。"""
    return """
(() => {
  const DATA = %s;
  const cur = location.origin;
  for (const o of DATA) {
    if (o.origin !== cur) continue;
    for (const it of (o.localStorage || [])) {
      try { localStorage.setItem(it.name, it.value); } catch (e) {}
    }
  }
})();
""" % json.dumps(origins or [], ensure_ascii=True)


class LocalWindowManager:
    """管理本机可见窗口(按 account_id 一一对应)。"""

    def __init__(self):
        self.windows = {}       # account_id -> dict(ctx/page/profile/temp/idle/task)

    # ---------- 打开 ----------
    async def open(self, manager, account, pw):
        aid = account["id"]
        if aid in self.windows:
            return {"ok": True, "already": True, "hint": "该账号的窗口已经打开了"}
        if len(self.windows) >= MAX_WINDOWS:
            return {"ok": False, "error": f"本机窗口最多同时开 {MAX_WINDOWS} 个,请先关掉一个"}

        worker = (getattr(manager, "workers", {}) or {}).get(aid)
        worker_ctx = getattr(worker, "context", None) if worker else None

        state = None
        temp_profile = None
        if worker_ctx is not None:
            # 情形 1:worker 活着 -> 只读导出登录态,用临时 profile 另起
            try:
                state = await worker_ctx.storage_state()
                n = len(state.get("cookies") or [])
                logger.info(f"[local-window] {aid} 从运行中的 context 导出登录态 cookies={n} 条")
            except Exception as e:
                logger.warning(f"[local-window] {aid} 导出登录态失败(改用账号 profile): {e}")
                state = None
            if state is not None:
                temp_profile = f"./profiles/_view_{aid}"
                _reset_dir(temp_profile)
        if temp_profile is None:
            # 情形 2:worker 没跑 -> profile 空着,直接用
            temp_profile = None

        profile_dir = temp_profile or account["profile_dir"]
        try:
            ctx = await launch_stealth(pw, profile_dir, headless=False,
                                       window_size=(WINDOW_W, WINDOW_H),
                                       window_position=(60, 40))
        except Exception as e:
            logger.warning(f"[local-window] {aid} 启动窗口失败: {e}")
            if temp_profile:
                shutil.rmtree(temp_profile, ignore_errors=True)
            return {"ok": False, "error": f"启动浏览器失败(profile 可能被占用): {e}"}

        # 注入登录态(cookies + localStorage)
        if state:
            try:
                ck = state.get("cookies") or []
                if ck:
                    await ctx.add_cookies(ck)
                origins = state.get("origins") or []
                if origins:
                    await ctx.add_init_script(_local_storage_script(origins))
                logger.info(f"[local-window] {aid} 已注入 cookies={len(ck)} origins={len(origins)}")
            except Exception as e:
                logger.warning(f"[local-window] {aid} 注入登录态失败: {e}")

        await ctx.add_init_script(ACTIVITY_SCRIPT)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.debug(f"[local-window] {aid} 首屏导航失败(窗口仍然可用): {e}")

        # 用户自己关窗口 -> 清理
        def _on_close(*_a):
            if aid in self.windows:
                self.windows[aid]["closed_by_user"] = True

        try:
            ctx.on("close", _on_close)
        except Exception:
            pass

        item = {
            "account_id": aid,
            "name": account.get("name") or aid,
            "profile_dir": profile_dir,
            "temp_profile": bool(temp_profile),
            "ctx": ctx,
            "page": page,
            "opened_at": time.time(),
            "last_active": time.time(),
            "closed_by_user": False,
            "injected": bool(state),
            "task": None,
        }
        item["task"] = asyncio.create_task(self._watchdog(aid))
        self.windows[aid] = item
        logger.info(f"[local-window] {aid} 本机窗口已打开 profile={profile_dir} "
                    f"injected={bool(state)}")
        return {"ok": True, "account_id": aid, "name": item["name"],
                "temp_profile": item["temp_profile"], "injected": item["injected"]}

    # ---------- 看门狗 ----------
    async def _watchdog(self, aid):
        try:
            while aid in self.windows:
                await asyncio.sleep(IDLE_CHECK_SEC)
                it = self.windows.get(aid)
                if not it:
                    return
                if it.get("closed_by_user"):
                    await self.close(aid, "用户关闭窗口")
                    return
                ts = it["last_active"]
                try:
                    pg = it["page"]
                    if pg is not None and not pg.is_closed():
                        v = await pg.evaluate("() => window.__sphgjLastAct || 0")
                        if v:
                            ts = v / 1000.0
                except Exception:
                    pass
                it["last_active"] = ts
                if time.time() - ts > IDLE_TIMEOUT_SEC:
                    logger.info(f"[local-window] {aid} 空闲超过 {IDLE_TIMEOUT_SEC}s,自动关闭")
                    await self.close(aid, "空闲超时")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[local-window] {aid} 看门狗异常: {e}")

    # ---------- 关闭 ----------
    async def close(self, aid, reason=""):
        it = self.windows.pop(aid, None)
        if not it:
            return {"ok": False, "error": "该账号没有打开的本机窗口"}
        t = it.get("task")
        if t is not None and not t.done():
            t.cancel()
        try:
            await close_context_safely(it["ctx"], it["profile_dir"], f"[local-window:{aid}]")
        except Exception as e:
            logger.debug(f"[local-window] {aid} 关闭 context 异常: {e}")
        if it.get("temp_profile"):
            p = it["profile_dir"]
            shutil.rmtree(p, ignore_errors=True)
            if os.path.isdir(p):
                logger.warning(f"[local-window] {aid} 临时 profile 未删净: {p}")
        logger.info(f"[local-window] {aid} 窗口已关闭({reason})")
        return {"ok": True, "account_id": aid, "reason": reason}

    async def close_all(self, reason="shutdown"):
        for aid in list(self.windows.keys()):
            try:
                await self.close(aid, reason)
            except Exception:
                pass

    # ---------- 查询 ----------
    def snapshot(self):
        now = time.time()
        return [{
            "account_id": it["account_id"],
            "name": it["name"],
            "opened_sec": int(now - it["opened_at"]),
            "idle_sec": int(now - it["last_active"]),
            "temp_profile": it["temp_profile"],
            "injected": it["injected"],
        } for it in self.windows.values()]


def _reset_dir(path):
    """清空并重建临时 profile 目录。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        logger.warning(f"[local-window] 重置临时目录失败 {path}: {e}")


def sweep_stale_view_profiles(log_tag="[local-window]"):
    """启动时清掉上次进程遗留的临时窗口 profile(_view_*)。"""
    base = "./profiles"
    if not os.path.isdir(base):
        return 0
    n = 0
    for name in os.listdir(base):
        if not name.startswith("_view_"):
            continue
        p = os.path.join(base, name)
        if not os.path.isdir(p):
            continue
        try:
            import psutil  # 有进程占着就跳过,别硬删
            busy = False
            ap = os.path.abspath(p)
            for proc in psutil.process_iter(["cmdline"]):
                for arg in (proc.info.get("cmdline") or []):
                    if arg.startswith("--user-data-dir=") and \
                            os.path.abspath(arg.split("=", 1)[1]) == ap:
                        busy = True
                        break
                if busy:
                    break
            if busy:
                continue
        except Exception:
            pass
        shutil.rmtree(p, ignore_errors=True)
        if not os.path.isdir(p):
            n += 1
    if n:
        logger.info(f"{log_tag} 清理遗留临时窗口 profile {n} 个")
    return n


local_windows = LocalWindowManager()
