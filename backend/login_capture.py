"""扫码登录会话:二维码截图推送 + 请求拦截自动抓取 _aid/_log_finder_id + 名称抓取。

流程:
  1. start(headless) 起 persistent context,goto 评论页(未登录跳登录页)
  2. 已登录(cookie 复用) -> 直接 finalize;否则进入二维码推送 + 等扫码
  3. 拦截 post_list 请求:URL 取 _aid,POST body 取 _log_finder_id
  4. 扫码成功(URL 回到评论页)-> 抓名称 -> 推 success + captured 字段
  5. 前端确认/编辑字段 -> finalize_with_id() 落盘 profile 改名 + 写 config
  6. 兜底 open_window():关 headless,同 profile_dir 重启 headed,拦截逻辑照跑
"""
import os
import re
import json
import uuid
import base64
import asyncio
from urllib.parse import urlparse, parse_qs
import logging
logger = logging.getLogger("sphgj")
from .browser import launch_stealth
from .selectors import QR_CANDIDATES, ACCOUNT_NAME_CANDIDATES, COMMENT_URL


class LoginSession:
    def __init__(self, playwright, config, emit, account=None):
        self.sid = uuid.uuid4().hex[:12]
        self.pw = playwright
        self.config = config
        self.emit = emit  # async emit(event:str, payload:dict)
        self.account = account  # relogin 模式:已有账号 dict;None=添加新账号
        self.context = None
        self.page = None
        self.profile_dir = None
        # pending|waiting_scan|scanned|capturing|success|failed|cancelled|open_window
        self.status = "pending"
        self.qr_image = None
        self.captured = {"aid": "", "finder_id": "",
                         "name": (account or {}).get("name", "")}
        self._tasks = []
        self._finalized = False

    # ---------- 生命周期 ----------
    async def start(self, headed=False):
        if self.account:
            # relogin:复用原账号 profile_dir(失效 cookie 会被扫码覆盖)
            self.profile_dir = self.account["profile_dir"]
        else:
            self.profile_dir = f"./profiles/_login_{self.sid}"
            os.makedirs(self.profile_dir, exist_ok=True)
        self.context = await launch_stealth(self.pw, self.profile_dir, headless=not headed)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("request", self._on_request)
        try:
            await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[login:{self.sid}] goto 评论页失败(可能跳登录,忽略): {e}")
        await self.page.wait_for_timeout(2500)
        if self._is_logged_in():
            logger.info(f"[login:{self.sid}] cookie 复用,已登录")
            self.status = "scanned"
            await self.emit("login_status", {"sid": self.sid, "status": "scanned"})
            await self._capture_fields_and_finalize()
            return
        self.status = "waiting_scan"
        await self.emit("login_status", {"sid": self.sid, "status": "waiting_scan"})
        # headed 默认:二维码在弹出的浏览器窗口显示,主弹窗不截图,无需 _qr_loop
        self._tasks.append(asyncio.create_task(self._wait_login_loop()))

    async def open_window(self):
        """兜底:关闭 headless,以同 profile_dir 重启 headed,让用户在弹出窗口扫码。"""
        headed_profile = self.profile_dir
        await self._close(keep_profile=True)
        self.context = await launch_stealth(self.pw, headed_profile, headless=False)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("request", self._on_request)
        try:
            await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        self.status = "waiting_scan"
        await self.emit("login_status", {"sid": self.sid, "status": "open_window"})
        self._tasks.append(asyncio.create_task(self._wait_login_loop()))

    async def cancel(self):
        self.status = "cancelled"
        await self._close(keep_profile=False)
        await self.emit("login_status", {"sid": self.sid, "status": "cancelled"})

    async def _close(self, keep_profile=False):
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
        if not keep_profile and self.profile_dir and not self._finalized:
            # 清理临时登录 profile
            try:
                import shutil
                if os.path.exists(self.profile_dir) and "_login_" in self.profile_dir:
                    shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception:
                pass

    # ---------- 请求拦截:抓 _aid / _log_finder_id ----------
    def _on_request(self, req):
        try:
            if "post/post_list" not in req.url:
                return
            qs = parse_qs(urlparse(req.url).query)
            aid = (qs.get("_aid") or [""])[0]
            if aid:
                self.captured["aid"] = aid
            body = req.post_data
            if body:
                try:
                    fid = json.loads(body).get("_log_finder_id")
                    if fid:
                        self.captured["finder_id"] = fid
                except Exception:
                    pass
            if self.captured["aid"] and self.captured["finder_id"]:
                logger.info(f"[login:{self.sid}] 抓到 _aid={aid} _log_finder_id={self.captured['finder_id'][:16]}…")
        except Exception as e:
            logger.debug(f"[login:{self.sid}] 拦截异常: {e}")

    # ---------- 二维码推送 ----------
    async def _qr_loop(self):
        last_png = None
        try:
            title = await self.page.title()
        except Exception:
            title = "?"
        logger.info(f"[login:{self.sid}] 二维码截图循环启动 URL={self.page.url} title={title}")
        no_qr_warned = False
        while self.status == "waiting_scan":
            png = await self._capture_qr()
            if png and png != last_png:
                last_png = png
                self.qr_image = png
                await self.emit("qr_update", {"sid": self.sid, "image": png})
            elif not png and not no_qr_warned:
                no_qr_warned = True
                logger.warning(f"[login:{self.sid}] 截图未取到任何图像,检查页面是否正常加载")
            await asyncio.sleep(2)

    async def _capture_qr(self):
        # 1. 优先按选择器截二维码元素
        for sel in QR_CANDIDATES:
            try:
                loc = self.page.locator(sel).first
                if await loc.count():
                    png = await loc.screenshot(timeout=3000)
                    logger.info(f"[login:{self.sid}] 二维码命中选择器: {sel}")
                    return "data:image/png;base64," + base64.b64encode(png).decode()
            except Exception as e:
                logger.debug(f"[login:{self.sid}] 选择器 {sel} 失败: {e}")
                continue
        # 2. fallback:选择器都没命中,截整页(真实窗口下必含二维码)
        try:
            png = await self.page.screenshot(timeout=5000)
            logger.info(f"[login:{self.sid}] 二维码选择器未命中,fallback 截整页")
            return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception as e:
            logger.warning(f"[login:{self.sid}] 整页截图失败: {e}")
            return None

    # ---------- 等扫码 ----------
    def _is_logged_in(self):
        url = self.page.url or ""
        # 扫码成功后视频号常先跳首页(platform/home)而非直接回评论页,
        # 只要离开了登录页即视为已登录,后续 _capture_fields_and_finalize 会主动跳评论页
        return ("channels.weixin.qq.com/platform" in url and "/login" not in url) if self.page else False

    async def _wait_login_loop(self):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 300
        while self.status == "waiting_scan" and loop.time() < deadline:
            if self._is_logged_in():
                self.status = "scanned"
                logger.info(f"[login:{self.sid}] 检测到登录成功(URL={self.page.url})")
                await self.emit("login_status", {"sid": self.sid, "status": "scanned"})
                await self._capture_fields_and_finalize()
                return
            await asyncio.sleep(2)
        if self.status == "waiting_scan":
            self.status = "failed"
            logger.warning(f"[login:{self.sid}] 扫码超时(5分钟)")
            await self.emit("login_status", {"sid": self.sid, "status": "failed", "error": "扫码超时(5分钟)"})

    async def _capture_fields_and_finalize(self):
        self.status = "capturing"
        await self.emit("login_status", {"sid": self.sid, "status": "capturing"})
        logger.info(f"[login:{self.sid}] 扫码成功,开始抓取字段(当前URL={self.page.url})")
        try:
            await asyncio.wait_for(self._do_capture(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[login:{self.sid}] 字段抓取超时(30s)")
        except Exception as e:
            logger.warning(f"[login:{self.sid}] 字段抓取异常: {e}")
        # 字段齐全 -> 通知前端自动保存(前端调 finalize 关弹窗 + 落盘);否则失败
        if self.captured["aid"] and self.captured["finder_id"]:
            self.status = "captured"
            logger.info(f"[login:{self.sid}] 抓取完成 aid/finder_id 齐全,通知前端保存 name={self.captured['name']}")
            await self.emit("login_status", {"sid": self.sid, "status": "captured",
                                             "captured": dict(self.captured)})
        else:
            self.status = "failed"
            logger.warning(f"[login:{self.sid}] 未抓到 _aid/_log_finder_id,无法保存")
            await self.emit("login_status", {"sid": self.sid, "status": "failed",
                                             "error": "未抓到账号信息,请重试"})

    async def _do_capture(self):
        """跳评论页 + 等 post_list 拦截 aid/finder_id + 抓名称。"""
        try:
            if not self._is_logged_in():
                logger.info(f"[login:{self.sid}] 未在评论页,跳转 {COMMENT_URL}")
                await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
            else:
                # 已在评论页:reload 触发 post_list 重发,确保拦截到 _aid/_log_finder_id
                try:
                    await self.page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[login:{self.sid}] 跳转评论页失败: {e}")
        # 等 post_list 拦截到 aid/finder_id(最多 15s)
        for _ in range(30):
            if self.captured["aid"] and self.captured["finder_id"]:
                break
            await asyncio.sleep(0.5)
        name = await self._capture_name()
        self.captured["name"] = name or self.captured["finder_id"][:12] or "未命名"

    async def _capture_name(self):
        for sel in ACCOUNT_NAME_CANDIDATES:
            try:
                loc = self.page.locator(sel).first
                if await loc.count():
                    txt = (await loc.text_content(timeout=2000) or "").strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return None

    # ---------- 前端确认后落盘 ----------
    async def finalize_with_id(self, account_id=None, name=None):
        """前端确认/编辑字段后调用。添加模式:profile 改名 + 写 config 新账号;
        relogin 模式:更新原账号字段(保留 id/profile_dir)。返回 account dict。"""
        if self._finalized:
            return None
        await self._close(keep_profile=True)
        if self.account:
            # relogin:更新原账号 _aid/_log_finder_id/name,保留 id 与 profile_dir
            acc = self.account
            acc["_aid"] = self.captured["aid"] or acc.get("_aid", "")
            acc["_log_finder_id"] = self.captured["finder_id"] or acc.get("_log_finder_id", "")
            acc["name"] = (name or self.captured["name"] or acc.get("name") or acc["id"]).strip()
            self._save_config()
            self._finalized = True
            logger.info(f"[login:{self.sid}] 账号已更新(relogin): {acc['id']} ({acc['name']})")
            return acc
        acc_id = (account_id or self._gen_id()).strip() or self._gen_id()
        final_dir = f"./profiles/{acc_id}"
        if self.profile_dir and os.path.exists(self.profile_dir) and self.profile_dir != final_dir:
            try:
                if os.path.exists(final_dir):
                    final_dir = f"./profiles/{acc_id}_{self.sid[:4]}"
                os.rename(self.profile_dir, final_dir)
            except Exception as e:
                logger.error(f"[login:{self.sid}] profile 改名失败: {e}")
                final_dir = self.profile_dir
        acc = {
            "id": acc_id,
            "name": (name or self.captured["name"] or acc_id).strip(),
            "_aid": self.captured["aid"],
            "_log_finder_id": self.captured["finder_id"],
            "profile_dir": final_dir,
            "auto_comment_enabled": False,
            "auto_comment_content": "",
        }
        self.config.setdefault("accounts", []).append(acc)
        self._save_config()
        self._finalized = True
        logger.info(f"[login:{self.sid}] 账号已保存: {acc_id} ({acc['name']})")
        return acc

    def _gen_id(self):
        n = len(self.config.get("accounts", [])) + 1
        base = self.captured.get("name") or "acc"
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", base).lower()[:12] or "acc"
        return f"{slug}{n}"

    def _save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
