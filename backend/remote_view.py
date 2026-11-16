"""远程同步浏览:把账号的浏览器页面串流到前端标签页,并支持鼠标/键盘操作。

为什么这么做
-----------
「打开」按钮原先是起一个 headed 窗口让用户在本机桌面上点 —— WEB 端用户看不到,
本机用户也得挤到主机前。改成 CDP 串流后,本机与 WEB 端走同一条通道:
  Page.startScreencast(JPEG)  -> WebSocket 二进制帧 -> 前端 canvas
  前端鼠标/键盘             -> Input.dispatchMouseEvent / insertText -> 页面

资源与抓取的关系(重要)
--------------------
rev66: 改用独立临时 profile + 注入登录态,**不停 worker、不动运行意图**。
旧实现共用一个 profile_dir,必须先 _park_worker 让锁;WS 在 hello 之前断开
会留下僵尸会话,账号一直停到看门狗 5 分钟后才恢复。现在三条「另起浏览器」
入口(本机窗口 / 大屏 / 远程串流)各用各的临时 profile,可并存且不影响采集。

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
from .local_window import (export_login_state, inject_login_state,
                           new_view_profile, drop_view_profile,
                           _local_storage_script)

logger = logging.getLogger("sphgj")

VIEW_W, VIEW_H = 1280, 800
JPEG_QUALITY = 58
IDLE_TIMEOUT_SEC = 300          # 5 分钟无操作自动断开
DASHBOARD_IDLE_SEC = 7200       # rev67: 直播大屏是挂着看的,2 小时无操作才断
DEFAULT_MAX_SESSIONS = 4  # rev74: 放宽到 4 路,可在 config.json 的 remote.max_sessions 调
MAX_SESSIONS = DEFAULT_MAX_SESSIONS          # 兼容别名(老脚本可能引用)
                                # 每路 context 约 150-300MB,上限由 max_sessions() 兜底
DEFAULT_URL = "https://channels.weixin.qq.com/platform/post/create"
HOME_URL = "https://channels.weixin.qq.com/platform"


class SessionOccupied(RuntimeError):
    """rev74: 同账号已有一路活跃串流,且本次连接不允许接管。"""


class SessionOccupied(RuntimeError):
    """rev74: 同账号已有一路活跃串流,且本次连接不允许接管。"""


class SessionOccupied(RuntimeError):
    """rev74: 同账号已有一路活跃串流,且本次连接不允许接管。"""


class RemoteSession:
    """一路串流会话:一个浏览器 context + 一个 page + 一条 CDP 通道。"""

    def __init__(self, manager, account_id, config, storage=None,
                 idle_timeout_sec=IDLE_TIMEOUT_SEC):
        self.manager = manager
        self.account_id = account_id
        self.config = config
        self.storage = storage
        self.ctx = None
        self.page = None
        self.cdp = None
        self.profile_dir = None     # rev66: 独立临时 profile
        self.w = VIEW_W
        self.h = VIEW_H
        self.url = ""
        self.last_active = time.time()
        self.idle_timeout_sec = int(idle_timeout_sec or IDLE_TIMEOUT_SEC)
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
        self.owner = None              # rev74: 当前持有这路串流的 WebSocket
        self.owner = None              # rev74: 当前持有这路串流的 WebSocket
        self.owner = None              # rev74: 当前持有这路串流的 WebSocket

    # ---------------- 生命周期 ----------------
    @property
    def acc(self):
        return next((a for a in self.config.get("accounts", [])
                     if a.get("id") == self.account_id), None)

    async def open(self, url=None):
        acc = self.acc
        if not acc:
            raise RuntimeError("账号不存在")
        # rev66: worker 一直活着,登录态优先从它的 context 现导(最权威),
        # 纯 HTTP 账号没有 context 时回落 config.storage_state。
        state = await export_login_state(self.manager, self.account_id, acc, "remote")
        if not state:
            raise RuntimeError("该账号没有可用登录态,请先扫码登录")

        pw = await self.manager._ensure_playwright()
        self.profile_dir = new_view_profile("%s_remote" % self.account_id)
        self.ctx = await launch_stealth(pw, self.profile_dir, headless=True)
        pages = list(self.ctx.pages)
        self.page = pages[0] if pages else await self.ctx.new_page()
        # 视频号身份 aid 存在 localStorage,只注 cookie 不够(rev37)。
        await inject_login_state(self.ctx, state, self.account_id, "remote")

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
                "idle_timeout": self.idle_timeout_sec,
                "logged_in": bool((self.acc or {}).get("_log_finder_id"))}

    async def _watchdog(self):
        """后端兜底:用户长时间没动作就收尾(前端崩了也关得掉)。"""
        try:
            while not self.closed:
                await asyncio.sleep(5)
                if time.time() - self.last_active > self.idle_timeout_sec + 10:
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
        self.owner = None              # rev74: 旧 WS 的 release 不再误关新会话
        self.owner = None              # rev74: 旧 WS 的 release 不再误关新会话
        self.owner = None              # rev74: 旧 WS 的 release 不再误关新会话
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
                await close_context_safely(self.ctx, self.profile_dir,
                                          f"remote-{self.account_id}")
            except Exception as e:
                logger.warning(f"[remote] 关 context 异常: {e}")
            self.ctx = None
        drop_view_profile(self.profile_dir)
        self.profile_dir = None
        # rev66: worker 从未停过,不需要(也不能)在这里 start_account ——
        # 那会重新写运行意图,把用户手动停的号又拉起来。
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
    """会话登记:同账号单路 + 归属管理(rev74)。

    rev74 之前同账号多标签会复用同一个 RemoteSession:两个 WS 从同一个
    maxsize=2 的帧队列抢帧(各只拿到一半画面),而且任一 WS 的 finally 都
    无条件 close(account),关掉一个标签页就把另一路一起断了。现在:
      * 每个 session 记 owner(持有它的 WebSocket);同账号第二个连接默认
        拒绝(SessionOccupied),只有 view=dashboard 允许接管并踢掉旧 WS;
      * release() 只在「会话仍是当初那个 且 owner 仍是自己」时收尾;
      * 上限可配(config.remote.max_sessions),踢旧按 created_at 并通知旧 WS。
    """

    def __init__(self, manager, config, storage=None):
        self.manager = manager
        self.config = config
        self.storage = storage
        self.sessions = {}

    def max_sessions(self):
        """同时最多几路串流;非法/缺省值一律收敛到 DEFAULT_MAX_SESSIONS。"""
        try:
            n = int(((self.config or {}).get("remote") or {}).get("max_sessions")
                    or DEFAULT_MAX_SESSIONS)
        except (TypeError, ValueError, AttributeError):
            n = DEFAULT_MAX_SESSIONS
        return max(1, min(16, n))

    def get(self, account_id):
        s = self.sessions.get(account_id)
        if s and not s.closed:
            return s
        return None

    def is_occupied(self, account_id, owner):
        s = self.get(account_id)
        return bool(s and getattr(s, "owner", None) is not None
                    and s.owner is not owner)

    @staticmethod
    async def _evict_owner(s):
        """接管/踢旧时让旧 WS 立刻断开,避免它继续抢帧或误关新会话。"""
        w = getattr(s, "owner", None)
        s.owner = None
        if w is None:
            return
        try:
            await w.send_json({"t": "closed", "reason": "kicked",
                               "message": "这个账号已在另一个标签页里打开,当前连接被接管"})
        except Exception:
            pass
        try:
            await w.close()
        except Exception:
            pass

    async def open(self, account_id, url=None, idle_timeout_sec=None,
                   owner=None, takeover=False):
        # 1) 同账号占用先判:不能先踢掉别人的会话再报错
        s = self.get(account_id)
        if s is not None and self.is_occupied(account_id, owner):
            if not takeover:
                raise SessionOccupied(
                    "该账号已在另一个标签页里打开远程串流,请先关闭那个标签页再重开")
            # 直播大屏按钮本身就是新标签页(rev67),允许它接管旧连接。
            await self._evict_owner(s)

        # 2) 全局上限:按创建时间踢最旧的;排除本账号(它可能正要被复用),
        #    否则复用自己的会话时会把自己踢掉,大屏画面直接丢失。
        others = sorted((x for x in self.sessions.values()
                         if not x.closed and x.account_id != account_id),
                        key=lambda x: getattr(x, "created_at", 0))
        while len(others) >= self.max_sessions():
            old = others.pop(0)
            logger.info(f"[remote] 会话数已满,先关掉 {old.account_id}")
            await self._evict_owner(old)
            await old.close("kicked")
            self.sessions.pop(old.account_id, None)

        s = self.get(account_id)
        if s is not None:
            # rev66: 页面已关 / CDP 掉了的「僵尸会话」不能复用,否则前端
            # 连上就报「建立会话失败」,而且这个会话永远没人收尾。
            try:
                if (s.page is not None and not s.page.is_closed()
                        and s.cdp is not None and s.ctx is not None):
                    await s.refresh_url()
                    s.owner = owner
                    return s, True
            except Exception:
                pass
            logger.info(f"[remote] 复用会话已失效,重建 {account_id}")
            await self.close(account_id, "stale")
        s = RemoteSession(self.manager, account_id, self.config, self.storage,
                          idle_timeout_sec=idle_timeout_sec or IDLE_TIMEOUT_SEC)
        try:
            await s.open(url)
        except Exception:
            # 启动浏览器/注入登录态失败时,别留下半死会话(rev66 的僵尸来源)
            await s.close("open_failed")
            raise
        s.owner = owner
        self.sessions[account_id] = s
        return s, False

    async def close(self, account_id, reason="closed"):
        s = self.sessions.pop(account_id, None)
        if s is not None:
            await s.close(reason)

    async def release(self, account_id, sess, owner, reason="closed"):
        """rev74: WS 结束时只收尾「自己仍持有」的那一路。

        被接管 / 被踢旧 / 已被重建的旧 WS 走到这里时,owner 或 session
        对象都不再匹配,直接放过,不会把新标签页的串流一起关掉。
        """
        if sess is None:
            return
        if self.sessions.get(account_id) is not sess:
            return
        if getattr(sess, "owner", None) is not owner:
            return
        await self.close(account_id, reason)

    async def close_all(self):
        for aid in list(self.sessions):
            await self.close(aid, "shutdown")

    def snapshot(self):
        return [{"account_id": s.account_id, "url": s.url,
                 "idle_sec": int(time.time() - s.last_active),
                 "created_at": s.created_at,
                 "owned": getattr(s, "owner", None) is not None}
                for s in self.sessions.values() if not s.closed]
