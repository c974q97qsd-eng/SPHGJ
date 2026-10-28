# -*- coding: utf-8 -*-
"""抖音扫码登录会话。

与视频号 LoginSession 的关系：**只借协议，不借实现**。

借的协议（前端 AddAccountDialog 已经在用，不用改前端协议）：
    emit("login_status", {"sid", "status", "error"?, "account"?})
    emit("qr_update",   {"sid", "image"})          # image 是可直接塞 <img src> 的 data URL
    emit("accounts_updated", {"reason", "sid"})
status 取值与视频号一致：pending / waiting_scan / scanned / captured / failed / cancelled

不借实现的原因：视频号那套里「微信锁定校验」「选择视频号」「auth_data 硬验」
「拦截 /connect/qrcode/ 拿原图」等逻辑抖音一条都用不上，硬套只会两头都难改。

======================================================================
实测结论（2026-10-28，隔离浏览器上下文，不是照文档猜的）
======================================================================
1. 未登录访问 https://creator.douyin.com/creator-micro/home **不跳转**，
   同一个页面里渲染登录卡片（约 726x515，class 含 "login"）。
   → 不需要处理重定向、也没有 passport iframe。
2. 二维码是 <img>，`src` 本身就是 `data:image/png;base64,…`
   （naturalWidth/Height = 512x512 原图，base64 整串只有约 2.9KB），
   页面里 canvas 数量 = 0。
   → **直接取 src 推给前端即可**：不截图、不裁深色背景，比视频号那套干净得多。
3. ⚠️ 二维码 img 的 class 是随机 hash（实测 RhjdbXj8，刷新会变），
   **绝对不能用 class 选择器定位**。只能靠「登录卡片内 + 近似方形 + 显示尺寸 120~320」。
4. 登录态 cookie：sessionid / sessionid_ss / sid_tt / uid_tt / uid_tt_ss /
   sid_guard / sid_ucp_v1 / ssid_ucp_v1。
   → 用 sessionid 做**轻量轮询信号**（读 cookie，不发网络请求），
     命中后必须再用 work_list **硬验**一次。

======================================================================
🚨 profile 目录策略：不搬、不造临时目录
======================================================================
新账号在**登录开始前**就分配好 id（`dy_` + 6 位 hex），登录直接落进
`./profiles/<acc_id>`，登录成功后**只更新名字/身份字段，不动目录**。

为什么不学视频号「临时目录登录 → 成功后迁移」：
  · 迁移要在 context 关闭后 rename，目标被残留 Chromium 占着就失败；
  · 视频号 rev32 就是在这里降级成「把临时目录写进 config」，临时目录一清
    账号就永久登录不上（9/10 号事故）。
  · 抖音没有「先扫码再决定 id」的需求（一个抖音号对应一次登录，不像视频号
    扫码后才能知道是哪个号），所以完全可以把 id 提前定好，绕开整个问题。
"""
import asyncio
import base64
import datetime
import json
import logging
import os
import re
import time
import uuid

from ..browser import launch_stealth, close_context_safely, reap_profile_orphans
from . import api
from .client import DouyinClient

logger = logging.getLogger("sphgj")

DY_HOME = "https://creator.douyin.com/creator-micro/home"
PROFILE_ROOT = "./profiles"

QR_WAIT_SEC = 300.0        # 扫码总超时（5 分钟，与视频号一致）
QR_POLL_SEC = 1.8          # 二维码轮询间隔（变了才推）
LOGIN_CHECK_SEC = 2.0      # 登录态轻量检查间隔
SCANNED_TEXT = ("确认登录", "请在手机上确认", "扫描成功")

#: 只有这几个 cookie 同时存在才算「可能已登录」。sessionid 是抖音的登录票据。
SESSION_COOKIES = ("sessionid", "sessionid_ss")

#: 在登录卡片里找二维码：卡片内 + 近似方形 + 显示尺寸 120~320 + 有 src。
#: 不能用 class（随机 hash）。优先取显示尺寸最大的那个。
JS_FIND_QR = r"""
(() => {
  const cards = [...document.querySelectorAll('div[class*="login"]')].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 300 && r.height >= 300;
  });
  cards.sort((a, b) => {
    const A = a.getBoundingClientRect(), B = b.getBoundingClientRect();
    return B.width * B.height - A.width * A.height;
  });
  const scope = cards.length ? cards[0] : document.body;
  let best = null;
  for (const el of scope.querySelectorAll('img')) {
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 120 || r.width > 320) continue;
    if (Math.abs(r.width - r.height) > Math.max(10, r.width * 0.1)) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const src = el.getAttribute('src') || '';
    if (!src) continue;
    if (!best || r.width > best.w) best = {w: r.width, h: r.height, src: src};
  }
  return best ? JSON.stringify(best) : '';
})()
"""

#: 取二维码元素句柄（src 不是 data: 时的兜底：只截这个元素，不截整页）
JS_QR_HANDLE = r"""
(() => {
  const cards = [...document.querySelectorAll('div[class*="login"]')].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 300 && r.height >= 300;
  });
  const scope = cards.length ? cards[0] : document.body;
  let best = null;
  for (const el of scope.querySelectorAll('img,canvas')) {
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 120 || r.width > 320) continue;
    if (Math.abs(r.width - r.height) > Math.max(10, r.width * 0.1)) continue;
    if (!best || r.width > best.getBoundingClientRect().width) best = el;
  }
  return best;
})()
"""

#: 页面上有没有「请确认登录」之类的提示（用于只读感知，不做登录判定）
JS_PAGE_TEXT = r"""
(() => {
  const t = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
  return t.slice(0, 600);
})()
"""


def _safe_name(s, fallback="dy"):
    s = re.sub(r"[^\w\u4e00-\u9fa5-]", "", (s or "")).strip()
    return s[:16] or fallback


class DyLoginSession:
    """一次抖音扫码登录。生命周期：start() -> (qr_update…) -> captured / failed / cancelled。"""

    def __init__(self, playwright, config, emit, account=None, save_config=None,
                 auto_finalize=True, on_finalized=None):
        self.sid = uuid.uuid4().hex[:12]
        self.pw = playwright
        self.config = config if isinstance(config, dict) else {}
        self.emit = emit
        self.account = account            # 有值 = 重新登录（沿用原 id 与 profile_dir）
        self._save_config_cb = save_config
        self.auto_finalize = auto_finalize
        #: 登录成功落盘后的回调（服务器用它自动拉起 worker 开轮询）。
        #: 必须容错：回调炸了不能影响登录结果本身。
        self.on_finalized = on_finalized
        self.context = None
        self.page = None
        self.profile_dir = None
        self.status = "pending"
        self.qr_image = None
        self._last_qr = ""
        self._storage_state = None
        self._tasks = []
        self._finalized = False
        self._headed = False
        self.error = ""
        self.identity = {}
        # 新账号：登录前就把 id 定好，登录直接落进最终目录（不搬、不造临时目录）
        if self.account:
            self.acc_id = self.account.get("id") or ""
        else:
            self.acc_id = "dy_" + uuid.uuid4().hex[:6]
        self.profile_dir = (self.account or {}).get("profile_dir") or \
            os.path.join(PROFILE_ROOT, self.acc_id)

    # ---------------- 对外 ----------------
    def snapshot(self):
        return {"sid": self.sid, "status": self.status, "error": self.error,
                "account_id": self.acc_id, "profile_dir": self.profile_dir,
                "has_qr": bool(self.qr_image), "identity": dict(self.identity)}

    async def start(self, headed=False):
        self._headed = headed
        os.makedirs(PROFILE_ROOT, exist_ok=True)
        # 复用该目录前先清孤儿：残留 Chromium 独占 profile 时，扫码写进去的 cookie
        # 落不了盘，关掉就没了 —— 表现是「扫码成功，一启动又回登录页」。
        reap_profile_orphans(self.profile_dir, log_tag=f"[dylogin:{self.sid}] 扫码前")
        os.makedirs(self.profile_dir, exist_ok=True)

        self.context = await launch_stealth(self.pw, self.profile_dir, headless=not headed)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        try:
            await self.page.goto(DY_HOME, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[dylogin:{self.sid}] 打开抖音创作中心失败(忽略): {e}")
        await self.page.wait_for_timeout(2500)

        # cookie 还能用 -> 不弹码，直接走硬验
        if await self._has_session():
            logger.info(f"[dylogin:{self.sid}] cookie 复用，已是登录态")
            await self._on_session_found(reused=True)
            return

        self.status = "waiting_scan"
        await self.emit("login_status", {"sid": self.sid, "status": "waiting_scan",
                                        "platform": "dy"})
        if not headed:
            # headed 模式二维码就在弹出的窗口里，不用推图
            self._tasks.append(asyncio.create_task(self._qr_loop()))
        self._tasks.append(asyncio.create_task(self._wait_login_loop()))

    async def cancel(self):
        self.status = "cancelled"
        await self._close()
        await self.emit("login_status", {"sid": self.sid, "status": "cancelled",
                                        "platform": "dy"})

    async def finalize(self, name=None):
        """手动落盘（auto_finalize=False 时用）。返回 account dict 或 None。"""
        if self._finalized:
            return None
        return await self._finalize(name=name)

    # ---------------- 内部 ----------------
    async def _close(self):
        # 🚨 必须跳过当前 task：_finalize() 是在 _wait_login_loop 里被 await 的，
        # 它调 _close() 时如果把自己也 cancel 掉，下一次 await 就抛 CancelledError，
        # 登录最后一步（写 config / 推 captured 事件）会随机丢。
        cur = asyncio.current_task()
        for t in self._tasks:
            if t is cur:
                continue
            if not t.done():
                t.cancel()
        self._tasks = []
        ctx, self.context = self.context, None
        self.page = None
        if ctx is not None:
            await close_context_safely(ctx, self.profile_dir, f"[dylogin:{self.sid}]")

    async def _has_session(self):
        """轻量登录信号：只读 cookie，不发网络请求。

        sessionid 是抖音的登录票据。注意这里**只是信号**，命中后仍要 _hard_verify()，
        因为 cookie 可能是失效残留（守 rev54/55 的铁律：不靠 URL、不靠 DOM 判登录）。
        """
        if self.context is None:
            return False
        try:
            cookies = await self.context.cookies()
        except Exception:
            return False
        names = {c.get("name") for c in (cookies or [])
                 if "douyin.com" in (c.get("domain") or "")}
        return all(n in names for n in SESSION_COOKIES)

    async def _hard_verify(self):
        """用 work_list 硬验登录态。返回 (是否登录, 身份 dict)。

        status_code=8 = 未登录（抖音系通用）；只要有一枪真的打进去（有 extra 字段），
        就算登录成功 —— 新号没有作品也照样算成功。
        """
        if self.page is None:
            return False, {}
        client = DouyinClient(self.page, {"id": self.acc_id}, config=self.config)
        try:
            env = await client.fetch_work_page(max_cursor=0, count=1)
        except Exception as e:
            logger.debug(f"[dylogin:{self.sid}] 硬验异常: {e}")
            return False, {}
        if not env.get("__ok"):
            return False, {}
        ident = {}
        for w in (env.get("works") or []):
            a = w.get("author") or {}
            if a.get("uid") or a.get("sec_uid"):
                ident = {"uid": str(a.get("uid") or ""),
                         "sec_uid": str(a.get("sec_uid") or ""),
                         "unique_id": str(a.get("unique_id") or ""),
                         "nickname": a.get("nickname") or ""}
                break
        if not ident:
            # 没作品的新号拿不到 author：至少把 work_list 通了这件事当证据，
            # 身份下一步靠 refresh_identity 补（owner 先退化成 acct:<id>，不猜）
            try:
                i2 = await client.self_identity()
                if i2:
                    ident = {"uid": str(i2.get("uid") or ""),
                             "sec_uid": str(i2.get("sec_uid") or ""),
                             "unique_id": str(i2.get("unique_id") or ""),
                             "nickname": i2.get("nickname") or ""}
            except Exception:
                pass
        return True, ident

    async def _qr_loop(self):
        """轮询二维码，变了才推。顺带把「已扫码待确认」的提示感知一下。"""
        t0 = time.time()
        while self.status == "waiting_scan" and time.time() - t0 < QR_WAIT_SEC:
            try:
                img = await self._capture_qr()
            except Exception as e:
                logger.debug(f"[dylogin:{self.sid}] 取二维码失败: {e}")
                img = ""
            if img and img != self._last_qr:
                self._last_qr = img
                self.qr_image = img
                await self.emit("qr_update", {"sid": self.sid, "image": img,
                                              "platform": "dy"})
                logger.info(f"[dylogin:{self.sid}] 推送二维码 {len(img)}B")
            await asyncio.sleep(QR_POLL_SEC)

    async def _capture_qr(self):
        """取二维码。优先直接读 img.src（实测就是 data:image/png;base64），
        兜底才对该元素单独截图（绝不截整页）。"""
        if self.page is None:
            return ""
        try:
            raw = await self.page.evaluate(JS_FIND_QR)
        except Exception:
            return ""
        if raw:
            try:
                d = json.loads(raw)
            except Exception:
                d = {}
            src = (d or {}).get("src") or ""
            if src.startswith("data:image"):
                return src
            if src.startswith("http"):
                # 极少数情况下 src 是 http 图：直接交给前端（公开 CDN 可加载）。
                # 要带 cookie 才能取的话，下面兜底截图会更稳。
                pass
        # 兜底：只截二维码元素本身
        try:
            handle = await self.page.evaluate_handle(JS_QR_HANDLE)
            el = handle.as_element() if handle is not None else None
            if el is None:
                return ""
            png = await el.screenshot(type="png")
            if not png or len(png) < 200:
                return ""
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        except Exception:
            return ""

    async def _wait_login_loop(self):
        """轮询登录态：cookie 信号 -> 硬验 -> 成功。"""
        t0 = time.time()
        scanned_notified = False
        while self.status == "waiting_scan" and time.time() - t0 < QR_WAIT_SEC:
            await asyncio.sleep(LOGIN_CHECK_SEC)
            if self.status != "waiting_scan":
                return
            if not scanned_notified:
                # 只读感知「已扫码待确认」，纯 UX，不参与判定
                try:
                    txt = await self.page.evaluate(JS_PAGE_TEXT) if self.page else ""
                except Exception:
                    txt = ""
                if txt and any(k in txt for k in SCANNED_TEXT):
                    scanned_notified = True
                    await self.emit("login_status", {"sid": self.sid,
                                                    "status": "scanned",
                                                    "platform": "dy"})
            if not await self._has_session():
                continue
            await self._on_session_found()

        if self.status == "waiting_scan":
            self.status = "failed"
            self.error = "扫码超时(5分钟)"
            await self._close()
            await self.emit("login_status", {"sid": self.sid, "status": "failed",
                                            "error": self.error, "platform": "dy"})

    async def _on_session_found(self, reused=False):
        """cookie 有了 -> 硬验 -> 成功落盘 / 失败继续等。"""
        if self.status in ("captured", "failed", "cancelled"):
            return
        ok, ident = await self._hard_verify()
        if not ok:
            # cookie 有了但接口还认不出来：可能是扫码中间的临时态，继续等（不算失败）
            logger.info(f"[dylogin:{self.sid}] 有 sessionid 但硬验未通过，继续等待")
            if reused:
                self.status = "failed"
                self.error = "本地 cookie 已失效，请重新扫码"
                await self._close()
                await self.emit("login_status", {"sid": self.sid, "status": "failed",
                                                "error": self.error, "platform": "dy"})
            return
        self.status = "scanned"
        await self.emit("login_status", {"sid": self.sid, "status": "scanned",
                                        "platform": "dy"})
        self.identity = dict(ident)
        # 扫码成功后必须存 storage_state（cookie 是延迟批量写盘的，
        # 只靠 profile 磁盘一旦进程被杀就丢，这是视频号踩过的坑）
        try:
            self._storage_state = await self.context.storage_state()
        except Exception as e:
            logger.warning(f"[dylogin:{self.sid}] storage_state 导出失败(忽略): {e}")
            self._storage_state = None
        if self.auto_finalize:
            await self._finalize()

    async def _finalize(self, name=None):
        if self._finalized:
            return None
        self._finalized = True
        await self._close()          # 先关 context：profile 释放后才能被 worker 使用

        ident = self.identity or {}
        nickname = (name or ident.get("nickname") or "").strip()
        login_at = datetime.datetime.now().isoformat(timespec="seconds")

        dup = None
        if not self.account and ident.get("sec_uid"):
            # 同一个抖音号重复扫码 -> 更新原账号，不新增
            dup = next((a for a in self.config.get("accounts", [])
                        if a.get("_dy_sec_uid") and a["_dy_sec_uid"] == ident["sec_uid"]),
                       None)

        if self.account:
            acc = self.account
            acc["_dy_uid"] = ident.get("uid") or acc.get("_dy_uid", "")
            acc["_dy_sec_uid"] = ident.get("sec_uid") or acc.get("_dy_sec_uid", "")
            acc["_dy_unique_id"] = ident.get("unique_id") or acc.get("_dy_unique_id", "")
            if nickname:
                acc["name"] = _safe_name(nickname, acc.get("name") or acc["id"])
            acc["login_at"] = login_at
        elif dup is not None:
            acc = dup
            old_dir = acc.get("profile_dir") or ""
            acc["_dy_uid"] = ident.get("uid") or acc.get("_dy_uid", "")
            acc["_dy_sec_uid"] = ident.get("sec_uid") or acc.get("_dy_sec_uid", "")
            acc["_dy_unique_id"] = ident.get("unique_id") or acc.get("_dy_unique_id", "")
            if nickname:
                acc["name"] = _safe_name(nickname, acc.get("name") or acc["id"])
            acc["profile_dir"] = self.profile_dir
            acc["login_at"] = login_at
            if old_dir and os.path.abspath(old_dir) != os.path.abspath(self.profile_dir):
                # 这次登录用的是新目录，旧目录已作废：先清孤儿再删（删不掉只告警）
                reap_profile_orphans(old_dir, log_tag=f"[dylogin:{self.sid}] 旧目录清理",
                                     min_age_sec=0)
                try:
                    import shutil
                    shutil.rmtree(old_dir, ignore_errors=True)
                except Exception:
                    pass
        else:
            acc = {
                "id": self.acc_id,
                "platform": "dy",
                "name": _safe_name(nickname, "抖音账号" + self.acc_id[-4:]),
                "_dy_uid": ident.get("uid") or "",
                "_dy_sec_uid": ident.get("sec_uid") or "",
                "_dy_unique_id": ident.get("unique_id") or "",
                "profile_dir": self.profile_dir,
                "login_at": login_at,
            }
            self.config.setdefault("accounts", []).append(acc)

        if self._storage_state and (self._storage_state.get("cookies") or []):
            acc["_dy_storage_state"] = self._storage_state

        self._save_config()
        self.status = "captured"
        await self.emit("login_status", {"sid": self.sid, "status": "captured",
                                        "platform": "dy", "account": acc})
        await self.emit("accounts_updated", {"reason": "dy_login_finalize",
                                            "sid": self.sid})
        logger.info(f"[dylogin:{self.sid}] 抖音账号已保存: {acc['id']} ({acc.get('name')}) "
                    f"sec_uid={(acc.get('_dy_sec_uid') or '')[:16]}…")
        # 扫完码得有人接着跑：交给服务器把 worker 拉起来
        if self.on_finalized:
            try:
                await self.on_finalized(acc)
            except Exception as e:
                logger.warning(f"[dylogin:{self.sid}] on_finalized 回调异常(忽略): {e!r}")
        return acc

    def _save_config(self):
        if self._save_config_cb:
            try:
                self._save_config_cb(self.config)
                return
            except Exception as e:
                logger.warning(f"[dylogin:{self.sid}] save_config 回调失败，改用直写: {e}")
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[dylogin:{self.sid}] config.json 写入失败: {e}")
