# -*- coding: utf-8 -*-
"""抖音账号运行容器：浏览器生命周期 + 在网状态机 + 抓取轮询。

与视频号 AccountWorker 的关系：**借状态机语义，不共用实现**。
视频号那 2400 行里直播检测 / 发布 / 自动回复占了绝大部分，抖音一条都用不上；
而且它的状态机和 live API 探测是深度耦合的，硬塞进来会把已经稳定运行的东西搞乱。

======================================================================
在网状态机（照搬 rev54/55 用血换来的四条前提，一条都不能少）
======================================================================
auth_state ∈ {unknown, ok, offline}

1. **判离线只认确定性证据**：work_list 返回 `status_code=8`（抖音明确的未登录）
   且身份硬验也拿不到。`-1` / 超时 / 页面异常一律**不判** —— 误判会把好账号
   踢出轮询，而它其实下一轮自己就好了。
2. **离线后 _loop 只等 _online_event**：不抢锁、不发包、不开浏览器。
3. **启动时未登录也必须落盘**（`_remember_offline`），否则 data/dy_auth_state.json
   永远是空的，用户每点一次「启动」都要白开一次 Chromium。
   ⚠️ 这条路径**不要**调 `mark_offline()` —— 它会去排队推二维码，
   和用户手上的扫码会话抢同一个 profile 目录。
4. **关闭 context 必须清 `_verify_result`**，否则会拿上一轮的过期证据判离线。

======================================================================
浏览器生命周期
======================================================================
· 开：`launch_stealth(profile_dir)` -> 注入 `_dy_storage_state` 里的 cookie
  -> 打开评论管理页 -> 硬验 -> `manager.set_page(page)`。
· 关：`close_context_safely` + `manager.set_page(None)`，
  否则 manager 会拿着死 page 白发请求。
· 绝不强杀 Chromium 再指望登录态还在（cookie 是延迟批量写盘的，kill 后磁盘可能是空的）。
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import time

from ..browser import launch_stealth, close_context_safely, reap_profile_orphans
from . import api

logger = logging.getLogger("sphgj")

AUTH_FILE = os.path.join("data", "dy_auth_state.json")

#: 抓取轮询间隔（秒）。风控阈值不公开，取比视频号更保守的默认值。
DEFAULT_INTERVAL = 420
#: 离线后等扫码恢复的轮询粒度（秒）
OFFLINE_WAIT = 30.0


def _auth_fp(acc):
    """登录态指纹 —— 用平台内**稳定身份**（sec_uid/uid），不用 cookie。

    cookie 每次登录都变，用它做指纹会导致「重启一次就白开一次浏览器」。
    sec_uid 才是抖音账号的稳定唯一标识。身份变了 = 用户重新扫过码，
    之前的离线记录自动作废。
    """
    key = "%s|%s" % ((acc or {}).get("_dy_sec_uid") or "",
                     (acc or {}).get("_dy_uid") or "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()


class DouyinWorker:
    def __init__(self, account, config, storage, manager, emit=None):
        self.account = account or {}
        self.account_id = str(self.account.get("id") or "")
        self.config = config or {}
        self.storage = storage
        self.manager = manager
        self.emit = emit
        self.context = None
        self.page = None
        self._pw = None
        self.auth_state = "unknown"       # unknown | ok | offline
        self.auth_reason = ""
        self._verify_result = None         # None / "ok" / "no"
        self._task = None
        self._online_event = asyncio.Event()
        self._running = False
        self._rounds = 0
        self.last_error = ""
        self._auth_offline = self._load_auth_state()

    # ================= 状态持久化 =================
    def _load_auth_state(self):
        try:
            if os.path.exists(AUTH_FILE):
                with open(AUTH_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception as e:
            logger.debug("抖音在网状态读取失败(忽略): %r" % e)
        return {}

    def _save_auth_state(self):
        try:
            d = os.path.dirname(AUTH_FILE)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = AUTH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._auth_offline, f, ensure_ascii=False, indent=2)
            os.replace(tmp, AUTH_FILE)
        except Exception as e:
            logger.warning("抖音在网状态落盘失败(忽略): %r" % e)

    def _is_known_offline(self):
        off = self._auth_offline.get(self.account_id)
        if not off:
            return False
        return off.get("fp") == _auth_fp(self.account)

    def _remember_offline(self, reason=""):
        """只落盘，不触发扫码排队（见文件头第 3 条）。"""
        self._auth_offline[self.account_id] = {"reason": reason or "", "ts": time.time(),
                                               "fp": _auth_fp(self.account)}
        self._save_auth_state()

    def _clear_offline(self):
        if self._auth_offline.pop(self.account_id, None) is not None:
            self._save_auth_state()

    # ================= 对外 =================
    def is_offline(self):
        return self.auth_state == "offline"

    def status(self):
        return {
            "account_id": self.account_id,
            "auth_state": self.auth_state,
            "auth_reason": self.auth_reason,
            "running": bool(self._task and not self._task.done()),
            "browser_open": self.context is not None,
            "rounds": self._rounds,
            "last_error": self.last_error,
            "known_offline": self._is_known_offline(),
        }

    async def start(self, playwright=None, force=False):
        """启动该账号：建浏览器 -> 硬验 -> 起轮询。返回 dict 说明结果。"""
        if self._task and not self._task.done():
            return {"ok": True, "already": True, "auth_state": self.auth_state}
        # 归属键按「第一个 dy 账号」算（manager 目前只持有一个客户端）。
        # 多账号时若不管，第二个账号的数据会被记到第一个账号名下 —— 必须挡住。
        if self.account_id != self.manager.account_id:
            msg = ("当前只支持单个抖音账号运行（manager 只持有首个 dy 账号的客户端）。"
                   "请先移除多余的抖音账号：%s" % self.account_id)
            self.last_error = msg
            logger.warning("[dy:%s] %s" % (self.account_id, msg))
            return {"ok": False, "error": msg, "need_scan": False}

        if not force and self._is_known_offline():
            self.auth_state = "offline"
            self.auth_reason = "上次已判掉线且登录态未变化"
            logger.info("[dy:%s] 已知离线，跳过启动（零发包）。要重试请带 force" % self.account_id)
            return {"ok": False, "offline": True, "need_scan": True,
                    "msg": self.auth_reason}

        if playwright is not None:
            self._pw = playwright
        if self._pw is None:
            return {"ok": False, "error": "没有 playwright 实例（服务端未就绪）"}

        ok = await self._open()
        if not ok:
            # 只有**确定性未登录**才叫「需要扫码」。浏览器起不来 / 网络不通
            # 只是「没验成」，让用户去扫码是白折腾（他也扫不出来）。
            sure_offline = (self.auth_state == "offline")
            return {"ok": False, "offline": sure_offline, "need_scan": sure_offline,
                    "msg": self.auth_reason or self.last_error}

        self._running = True
        self._task = asyncio.create_task(self._loop())
        return {"ok": True, "auth_state": self.auth_state}

    async def stop(self):
        self._running = False
        self._online_event.clear()
        t, self._task = self._task, None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self._close()
        self.auth_state = "unknown"
        self.auth_reason = ""
        return {"ok": True}

    # ================= 浏览器 =================
    async def _open(self):
        """建 context + 页面 + 硬验 + 注入 manager。返回是否已登录。"""
        self._verify_result = None
        profile_dir = self.account.get("profile_dir") or ""
        if not profile_dir:
            self.last_error = "账号没有 profile_dir，需要重新扫码"
            self.auth_state = "offline"
            self._remember_offline(self.last_error)
            return False
        reap_profile_orphans(profile_dir, log_tag="[dy:%s] 启动前" % self.account_id)
        headless = bool((self.config.get("risk_control") or {}).get("dy_headless", True))
        try:
            self.context = await launch_stealth(self._pw, profile_dir, headless=headless)
        except Exception as e:
            # 开不起来不是「登录态坏了」，只能算没验成 —— 不能判离线
            self.last_error = "启动浏览器失败: %r" % (e,)
            logger.error("[dy:%s] %s" % (self.account_id, self.last_error))
            self._verify_result = None
            return False

        # 注入扫码时导出的登录态（双保险：profile 磁盘 cookie 可能没写成功）
        try:
            ss = self.account.get("_dy_storage_state") or {}
            ck = ss.get("cookies") or []
            if ck:
                await self.context.add_cookies(ck)
                logger.info("[dy:%s] 已注入登录态 cookies=%d 条" % (self.account_id, len(ck)))
        except Exception as e:
            logger.warning("[dy:%s] 注入登录态失败(回退用 profile 磁盘): %r"
                           % (self.account_id, e))

        try:
            self.page = self.context.pages[0] if self.context.pages else \
                await self.context.new_page()
            await self.page.goto(api.PAGE_COMMENT, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning("[dy:%s] 打开评论页失败(继续硬验): %r" % (self.account_id, e))

        if self.page is None:
            self.last_error = "没有可用页面"
            return False

        self.manager.set_page(self.page)
        v = await self.manager.verify_login()
        if v.get("ok"):
            self._verify_result = "ok"
            await self._mark_online("work_list 硬验通过")
            await self.manager.refresh_identity()
            return True

        # 没验成：区分「确定未登录」和「只是没验成」
        self._verify_result = "no" if v.get("reason") == api.ERR_NOT_LOGGED_IN else None
        if self._verify_result == "no":
            self.auth_state = "offline"
            self.auth_reason = "抖音未登录（status_code=8）"
            self._remember_offline(self.auth_reason)
            logger.warning("[dy:%s] 未登录（status_code=8），需要重新扫码" % self.account_id)
        else:
            # 网络/超时/页面异常：不判离线，保持 unknown，留给下一轮重试
            self.auth_state = "unknown"
            self.auth_reason = "登录态未验成（%s），不判离线" % (v.get("reason") or "未知")
            logger.warning("[dy:%s] %s" % (self.account_id, self.auth_reason))
        self.last_error = self.auth_reason
        await self._emit_status()
        await self._close()
        return False

    async def _close(self):
        """关 context 并清证据。必须调 manager.set_page(None)，别让它拿着死 page。"""
        ctx, self.context = self.context, None
        self.page = None
        self._verify_result = None
        try:
            self.manager.set_page(None)
        except Exception:
            pass
        if ctx is not None:
            await close_context_safely(ctx, self.account.get("profile_dir") or "",
                                       "[dy:%s] 收回进程" % self.account_id)

    # ================= 状态切换 =================
    async def _mark_online(self, why=""):
        if self.auth_state != "ok":
            logger.info("[dy:%s] 在网状态 -> ok（%s）" % (self.account_id, why or ""))
        self.auth_state = "ok"
        self.auth_reason = ""
        self.last_error = ""
        self._clear_offline()
        self._online_event.set()
        await self._emit_status()

    async def mark_offline(self, reason=""):
        """判掉线：落盘 + 关浏览器 + 通知上层排队扫码。

        ⚠️ 只应在拿到**确定性证据**（status_code=8）后调用。
        """
        if self.auth_state == "offline":
            return
        self.auth_state = "offline"
        self.auth_reason = reason or "登录态失效"
        logger.warning("[dy:%s] 判为离线：%s" % (self.account_id, self.auth_reason))
        self._remember_offline(self.auth_reason)
        self._online_event.clear()
        await self._close()
        await self._emit_status()
        if self.emit:
            try:
                await self.emit("dy_auth_change", {"account_id": self.account_id,
                                                  "state": "offline",
                                                  "reason": self.auth_reason})
            except Exception:
                pass

    async def _emit_status(self):
        if not self.emit:
            return
        try:
            await self.emit("dy_status", {"account_id": self.account_id,
                                          "auth_state": self.auth_state,
                                          "auth_reason": self.auth_reason,
                                          "running": bool(self._task and not self._task.done())})
        except Exception:
            pass

    # ================= 轮询 =================
    def _interval(self):
        try:
            v = int((self.config.get("risk_control") or {}).get("dy_fetch_interval")
                    or self.config.get("dy_fetch_interval") or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            v = DEFAULT_INTERVAL
        return max(60, v)

    async def _loop(self):
        while self._running:
            try:
                if self.auth_state == "offline":
                    # 离线：不抢锁、不发包、不开浏览器，只等扫码恢复
                    try:
                        await asyncio.wait_for(self._online_event.wait(), OFFLINE_WAIT)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if self.context is None:
                    ok = await self._open()
                    if not ok:
                        # 不是确定性未登录 -> 隔一会儿再试；是 -> 进离线分支等扫码
                        await asyncio.sleep(OFFLINE_WAIT if self.auth_state == "offline" else 60)
                        continue
                await self._run_round()
                secs = self._interval() * random.uniform(0.85, 1.15)
                await asyncio.sleep(secs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = "%r" % (e,)
                logger.error("[dy:%s] 轮询异常: %r" % (self.account_id, e))
                await asyncio.sleep(60)

    async def _run_round(self):
        self._rounds += 1
        # 「上一轮已确定未登录」且 context 还开着 -> 直接判离线，零请求。
        # 不必再发一枪撞 status_code=8 凑证据（rev55 的第二条前提）。
        if self._verify_result == "no" and self.context is not None:
            await self.mark_offline("上一轮已确认未登录")
            return
        rep = await self.manager.fetch_now()
        if rep.get("__ok"):
            if self.auth_state != "ok":
                await self._mark_online("抓取成功")
            if not rep.get("complete"):
                self.last_error = "本轮不完整（有请求失败）"
            else:
                self.last_error = ""
            logger.info("[dy:%s] 第%d轮 作品=%s 检查=%s 新增=%s 跳过=%s 完整=%s"
                        % (self.account_id, self._rounds, rep.get("works"),
                           rep.get("checked"), rep.get("new"), rep.get("skipped"),
                           rep.get("complete")))
            return
        err = rep.get("__err")
        if err == "not_ready":
            # manager 的 page 丢了（比如别处 set_page(None)）：重开
            await self._close()
            return
        if err == api.ERR_NOT_LOGGED_IN:
            await self.mark_offline("抓取返回 status_code=8（未登录）")
            return
        self.last_error = "抓取失败: %s %s" % (err, rep.get("__msg") or "")
        logger.warning("[dy:%s] %s（不判离线）" % (self.account_id, self.last_error))
