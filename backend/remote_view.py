"""远程同步浏览:把账号的浏览器页面串流到前端标签页,并支持鼠标/键盘操作。

为什么这么做
-----------
「打开」按钮原先是起一个 headed 窗口让用户在本机桌面上点 —— WEB 端用户看不到,
本机用户也得挤到主机前。改成 CDP 串流后,本机与 WEB 端走同一条通道:
  Page.startScreencast(JPEG)  -> WebSocket 二进制帧 -> 前端 canvas
  前端鼠标/键盘             -> Input.dispatchMouseEvent / insertText -> 页面

资源与抓取的关系(重要)
--------------------
同一个 profile_dir 不能同时被两处占用(Chromium profile 锁),所以远程会话期间必须
先停 worker。但**不能走 stop_account**:它会写 human_stopped、清运行意图,
把「临时让位」当成「人工停止」,之后自动发作品就不会再拉起这个号。
这里用 _park_worker 只摘 workers 里的 worker,两样都不动;关闭时按原样恢复。

空闲超时
--------
用户 5 分钟没有任何操作(鼠标/键盘/滚轮/导航)自动断开并恢复抓取,防止一直挂着
白白停掉数据采集。前端倒计时 + 后端看门狗双保险(前端崩了后端也会收尾)。
"""
import asyncio
import base64
import logging
import os
import time

from .browser import launch_stealth, close_context_safely
from .local_window import _local_storage_script

logger = logging.getLogger("sphgj")

VIEW_W, VIEW_H = 1280, 800
JPEG_QUALITY = 58
IDLE_TIMEOUT_SEC = 300          # 5 分钟无操作自动断开
MAX_SESSIONS = 2                # 同时最多两路串流:一路别人开了还能再开一路,
                                # 又不至于把主机压垮(每个 context 约 150-300MB)
DEFAULT_URL = "https://channels.weixin.qq.com/platform/post/create"
HOME_URL = "https://channels.weixin.qq.com/platform"


async def _park_worker(manager, account_id):
    """临时摘掉 worker 让出 profile 锁,不改人工停止标记/运行意图。"""
    w = (getattr(manager, "workers", {}) or {}).pop(account_id, None)
    if w is not None:
        try:
            await w.stop()
        except Exception as e:
            logger.warning(f"[remote] 停 worker 失败 {account_id}: {e}")
    return w


class RemoteSession:
    """一路串流会话:一个浏览器 context + 一个 page + 一条 CDP 通道。"""

    def __init__(self, manager, account_id, config, storage=None):
        self.manager = manager
        self.account_id = account_id
        self.config = config
        self.storage = storage
        self.ctx = None
        self.page = None
        self.cdp = None
        self.w = VIEW_W
        self.h = VIEW_H
        self.url = ""
        self.last_active = time.time()
        self.created_at = time.time()
        self.closed = False
        self.close_reason = ""
        self._frame_q = asyncio.Queue(maxsize=2)
        self._was_running = False
        self._was_human_stopped = False
        self._navigating = False
        self._task = None
        self._min_frame_gap = 0.05          # 帧间隔下限(<=20fps),防止白烧带宽
        self._last_frame_ts = 0.0

    # ---------------- 生命周期 ----------------
    @property
    def acc(self):
        return next((a for a in self.config.get("accounts", [])
                     if a.get("id") == self.account_id), None)

    async def open(self, url=None):
        acc = self.acc
        if not acc:
            raise RuntimeError("账号不存在")
        self._was_running = self.account_id in (getattr(self.manager, "workers", {}) or {})
        self._was_human_stopped = self.account_id in getattr(
            self.manager, "human_stopped", set())

        # rev37: 必须在 park 之前把登录态导出来 —— park 会把 worker 的 context
        # 一起关掉,之后就再也拿不到了。而 profile 磁盘的 cookie 是不可靠的
        # (Chromium 延迟写盘,进程被中途关掉就是 0 条,见 rev29),只靠磁盘会出现
        # 「worker 明明登录着,远程窗口一打开却是扫码页」(2026-10-21 用户实证)。
        state = None
        try:
            _w = (getattr(self.manager, "workers", {}) or {}).get(self.account_id)
            _ctx = getattr(_w, "context", None) if _w else None
            if _ctx is not None:
                state = await _ctx.storage_state()
                logger.info(f"[remote] {self.account_id} 从运行中的 context 导出"
                            f"登录态 cookies={len(state.get('cookies') or [])} 条")
        except Exception as e:
            logger.warning(f"[remote] {self.account_id} 导出登录态失败: {e}")
            state = None
        if not (state or {}).get("cookies"):
            # worker 没跑 / 导出失败 -> 退回 config 里存的扫码登录态
            _ss = acc.get("storage_state") or {}
            if _ss.get("cookies"):
                state = _ss
                logger.info(f"[remote] {self.account_id} 使用 config 内保存的登录态 "
                            f"cookies={len(_ss.get('cookies') or [])} 条")

        await _park_worker(self.manager, self.account_id)

        pw = await self.manager._ensure_playwright()
        self.ctx = await launch_stealth(pw, acc["profile_dir"], headless=True)
        pages = list(self.ctx.pages)
        self.page = pages[0] if pages else await self.ctx.new_page()
        # rev37: 注入登录态(cookie + localStorage)。视频号的身份 aid 存在
        # localStorage,只注 cookie 不够 —— 缺了它页面照样把你当没登录。
        if state:
            try:
                _ck = state.get("cookies") or []
                if _ck:
                    await self.ctx.add_cookies(_ck)
                _origins = state.get("origins") or []
                if _origins:
                    await self.ctx.add_init_script(_local_storage_script(_origins))
                logger.info(f"[remote] {self.account_id} 已注入登录态 "
                            f"cookies={len(_ck)} origins={len(_origins)}")
            except Exception as e:
                logger.warning(f"[remote] {self.account_id} 注入登录态失败: {e}")

        self.page.set_default_timeout(20000)
        try:
            await self.page.goto(url or DEFAULT_URL, wait_until="domcontentloaded",
                                 timeout=45000)
        except Exception as e:
            logger.warning(f"[remote] 打开页面超时(继续串流): {e}")
        self.url = self.page.url

        self.cdp = await self.ctx.new_cdp_session(self.page)
        # headless 下页面默认按「窗口未聚焦」处理,Input.insertText / 键盘事件会被吞。
        # 打开焦点模拟,远程键盘才真的落进输入框。
        try:
            await self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except Exception as e:
            logger.debug(f"[remote] 焦点模拟开启失败: {e}")
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.page.on("framenavigated", self._on_nav)
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": JPEG_QUALITY,
            "maxWidth": VIEW_W, "maxHeight": VIEW_H, "everyNthFrame": 3})
        self._task = asyncio.create_task(self._watchdog())
        logger.info(f"[remote] 会话已开启 {self.account_id} url={self.url}")
        return self.meta()

    def meta(self):
        return {"t": "hello", "account_id": self.account_id,
                "account_name": (self.acc or {}).get("name") or self.account_id,
                "w": self.w, "h": self.h, "url": self.url,
                "idle_timeout": IDLE_TIMEOUT_SEC,
                "logged_in": bool((self.acc or {}).get("_log_finder_id"))}

    async def _watchdog(self):
        """后端兜底:用户长时间没动作就收尾(前端崩了也关得掉)。"""
        try:
            while not self.closed:
                await asyncio.sleep(5)
                if time.time() - self.last_active > IDLE_TIMEOUT_SEC + 10:
                    logger.info(f"[remote] 空闲超时自动关闭 {self.account_id}")
                    await self.close("idle")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[remote] 看门狗异常: {e}")

    async def close(self, reason="closed"):
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        if self._task:
            self._task.cancel()
            self._task = None
        try:
            if self.cdp is not None:
                await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if self.ctx is not None:
            try:
                await close_context_safely(self.ctx, self.acc and self.acc.get("profile_dir"),
                                          f"remote-{self.account_id}")
            except Exception as e:
                logger.warning(f"[remote] 关 context 异常: {e}")
            self.ctx = None
        # 恢复原本在跑的账号(原本就是人工停的 -> 保持停)
        if self._was_running:
            try:
                w = await self.manager.start_account(self.account_id)
                if w is not None and self._was_human_stopped:
                    self.manager.human_stopped.add(self.account_id)
                logger.info(f"[remote] 已恢复抓取 {self.account_id}")
            except Exception as e:
                logger.warning(f"[remote] 恢复抓取失败 {self.account_id}: {e}")
        logger.info(f"[remote] 会话结束 {self.account_id} reason={reason}")

    # ---------------- 帧 ----------------
    # 流控:【每帧立刻 ack】。试过"取走才 ack"的拉模式,结果是丢帧时被丢的那帧永远
    # 不 ack,浏览器就停在那儿不再推 —— 画面停在白屏不刷新,交互直接失效。
    # 所以速率靠 everyNthFrame(约 8fps) + JPEG 质量压制,队列只留最新一帧。
    async def _on_frame(self, ev):
        try:
            md = ev.get("metadata") or {}
            if md.get("deviceWidth"):
                self.w, self.h = int(md["deviceWidth"]), int(md["deviceHeight"])
            data = ev.get("data") or ""
            while True:
                try:
                    self._frame_q.put_nowait(data)
                    break
                except asyncio.QueueFull:
                    try:
                        self._frame_q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            sid = ev.get("sessionId")
            if sid is not None and self.cdp is not None:
                try:
                    await self.cdp.send("Page.screencastFrameAck", {"sessionId": sid})
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[remote] 帧处理异常: {e}")

    async def next_frame(self, timeout=1.0):
        """取一帧 jpeg bytes;超时返回 None。"""
        try:
            b64 = await asyncio.wait_for(self._frame_q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        try:
            return base64.b64decode(b64)
        except Exception:
            return None

    async def _on_nav(self, frame):
        try:
            if frame == self.page.main_frame:
                self.url = frame.url
        except Exception:
            pass

    async def refresh_url(self):
        try:
            self.url = self.page.url
        except Exception:
            pass
        return self.url

    # ---------------- 输入 ----------------
    def touch(self):
        self.last_active = time.time()

    async def _send(self, method, params):
        if self.closed or self.cdp is None:
            return
        try:
            await self.cdp.send(method, params)
        except Exception as e:
            logger.debug(f"[remote] {method} 失败: {e}")

    async def mouse(self, kind, x, y, button="left", dy=0, dx=0):
        self.touch()
        x, y = int(x), int(y)
        if kind == "move":
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        elif kind == "down":
            # 先补一个 move:不少前端组件靠 hover/mousemove 才挂上点击处理
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mousePressed", "x": x, "y": y, "button": button,
                              "buttons": 1, "clickCount": 1})
        elif kind == "up":
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseReleased", "x": x, "y": y, "button": button,
                              "buttons": 0, "clickCount": 1})
        elif kind == "wheel":
            await self._send("Input.dispatchMouseEvent",
                             {"type": "mouseWheel", "x": x, "y": y,
                              "deltaX": int(dx), "deltaY": int(dy)})

    async def text(self, s):
        self.touch()
        if s:
            await self._send("Input.insertText", {"text": s})

    # 特殊键 -> CDP 键码
    _KEYS = {
        "Enter": (13, "Enter", "\r"),
        "Tab": (9, "Tab", "\t"),
        "Backspace": (8, "Backspace", ""),
        "Escape": (27, "Escape", ""),
        "Delete": (46, "Delete", ""),
        "ArrowLeft": (37, "ArrowLeft", ""),
        "ArrowRight": (39, "ArrowRight", ""),
        "ArrowUp": (38, "ArrowUp", ""),
        "ArrowDown": (40, "ArrowDown", ""),
        "Home": (36, "Home", ""),
        "End": (35, "End", ""),
        "PageUp": (33, "PageUp", ""),
        "PageDown": (34, "PageDown", ""),
    }

    async def key(self, name):
        self.touch()
        spec = self._KEYS.get(name)
        if not spec:
            return
        vk, code, text = spec
        base = {"key": code, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk}
        await self._send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        if text:
            await self._send("Input.dispatchKeyEvent",
                             {"type": "char", "text": text, **base})
        await self._send("Input.dispatchKeyEvent", {"type": "keyUp", **base})

    async def nav(self, action, url=""):
        self.touch()
        try:
            if action == "back":
                await self.page.go_back(timeout=15000)
            elif action == "forward":
                await self.page.go_forward(timeout=15000)
            elif action == "reload":
                await self.page.reload(timeout=30000)
            elif action == "goto" and url:
                if not url.startswith("http"):
                    url = "https://" + url.lstrip("/")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            elif action == "home":
                await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.debug(f"[remote] nav {action} 异常: {e}")
        await self.refresh_url()


class RemoteRegistry:
    """会话登记:同账号复用,超并发踢旧的。"""

    def __init__(self, manager, config, storage=None):
        self.manager = manager
        self.config = config
        self.storage = storage
        self.sessions = {}

    def get(self, account_id):
        s = self.sessions.get(account_id)
        if s and not s.closed:
            return s
        return None

    async def open(self, account_id, url=None):
        # 全局只留 MAX_SESSIONS 路
        alive = [s for s in self.sessions.values() if not s.closed]
        while len(alive) >= MAX_SESSIONS:
            old = alive.pop(0)
            logger.info(f"[remote] 会话数已满,先关掉 {old.account_id}")
            await old.close("kicked")
            self.sessions.pop(old.account_id, None)
        s = self.get(account_id)
        if s is not None:
            await s.refresh_url()
            return s, True
        s = RemoteSession(self.manager, account_id, self.config, self.storage)
        meta = await s.open(url)
        self.sessions[account_id] = s
        return s, False

    async def close(self, account_id, reason="closed"):
        s = self.sessions.pop(account_id, None)
        if s is not None:
            await s.close(reason)

    async def close_all(self):
        for aid in list(self.sessions):
            await self.close(aid, "shutdown")

    def snapshot(self):
        return [{"account_id": s.account_id, "url": s.url,
                 "idle_sec": int(time.time() - s.last_active),
                 "created_at": s.created_at} for s in self.sessions.values() if not s.closed]
