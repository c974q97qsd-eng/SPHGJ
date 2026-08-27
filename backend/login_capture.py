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
from .browser import launch_stealth, close_context_safely
from .selectors import QR_CANDIDATES, ACCOUNT_NAME_CANDIDATES, COMMENT_URL, LOGIN_URL


class LoginSession:
    def __init__(self, playwright, config, emit, account=None, auto_finalize=False, on_finished=None):
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
                         "name": (account or {}).get("name", ""),
                         "wx_name": (account or {}).get("_wx_name", "")}
        self._tasks = []
        self._finalized = False
        self.auto_finalize = auto_finalize  # auto 模式:captured 后后端自动落盘,不等前端调 finalize
        self.on_finished = on_finished      # 终态回调(sid, status, account_id),供 auto 队列调度
        self._finished_called = False

    def _finished(self, status):
        """终态通知(防重复):auto 模式下触发 manager 推进下一个 relogin。"""
        if self._finished_called:
            return
        self._finished_called = True
        if self.on_finished:
            try:
                self.on_finished(self.sid, status,
                                 self.account["id"] if self.account else None)
            except Exception as e:
                logger.debug(f"[login:{self.sid}] on_finished 异常: {e}")

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
            # 直接开登录页:扫码登录场景几乎都是未登录,省去"评论页->重定向登录页"一跳。
            # cookie 仍有效时视频号会从此重定向回 /platform 首页,_is_logged_in 照样判定。
            await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[login:{self.sid}] goto 登录页失败(忽略): {e}")
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
        self._finished("cancelled")

    async def _close(self, keep_profile=False):
        cur = asyncio.current_task()
        for t in self._tasks:
            if t is not cur:  # 不取消当前 task(失败路径在 _wait_login_loop 内调 _close)
                t.cancel()
        self._tasks = []
        if self.context:
            await close_context_safely(self.context, self.profile_dir, f"[login:{self.sid}]")
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
            await self._close(keep_profile=False)
            self._finished("failed")

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
            logger.info(f"[login:{self.sid}] 抓取完成 aid/finder_id 齐全 name={self.captured['name']}")
            await self.emit("login_status", {"sid": self.sid, "status": "captured",
                                             "captured": dict(self.captured)})
            # 字段已抓全,立即关 headed context 释放进程(finalize 只做 profile 改名+写 config,
            # 不再用 context);手动模式下用户编辑名字/确认期间不再白开 headed 进程。
            # auto 模式 finalize_with_id 内的 _close 会因 context=None 幂等跳过。
            await self._close(keep_profile=True)
            if self.auto_finalize:
                # auto 模式:后端直接落盘(等同前端 finalize),成功后 on_finished 推进队列
                await self.finalize_with_id(name=self.captured["name"])
        else:
            self.status = "failed"
            logger.warning(f"[login:{self.sid}] 未抓到 _aid/_log_finder_id,无法保存")
            await self.emit("login_status", {"sid": self.sid, "status": "failed",
                                             "error": "未抓到账号信息,请重试"})
            await self._close(keep_profile=False)
            self._finished("failed")

    async def _do_capture(self):
        """抓 _aid/_log_finder_id + 账号名。实测两者均在 localStorage(同源页可直接读),
        无需跳评论页拦截 post_list:_aid=localStorage.__ml::aid(去 JSON 引号),
        _log_finder_id=localStorage.finder_username(与 post_list body 完全一致)。
        账号名用顶栏 .account-info .name(首页/评论页同一组件)。扫码后停在当前页即可,0 跳转。"""
        # 1. localStorage 直接取 _aid / _log_finder_id(扫码后当前页即有)
        vals = {}
        for _ in range(6):
            vals = await self.page.evaluate("""() => {
                const aid = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
                const fid = localStorage.getItem('finder_username') || '';
                let a = aid;
                try { a = JSON.parse(aid); } catch(e) { a = aid.replace(/^"|"$/g, ''); }
                return { aid: a, finder_id: fid };
            }""")
            if vals.get("aid") and vals.get("finder_id"):
                break
            await asyncio.sleep(0.5)
        if vals.get("aid"):
            self.captured["aid"] = vals["aid"]
        if vals.get("finder_id"):
            self.captured["finder_id"] = vals["finder_id"]
        logger.info(f"[login:{self.sid}] localStorage 取 _aid={self.captured['aid'][:20]} _log_finder_id={self.captured['finder_id'][:20]}")
        # 2. 顶栏抓账号名(.account-info .name)
        await self.page.wait_for_timeout(2500)
        dom_name = await self._capture_name()
        if dom_name:
            self.captured["name"] = dom_name
        # 3. fallback:localStorage 没拿全 _aid/finder_id -> 跳评论页拦截 post_list
        if not (self.captured["aid"] and self.captured["finder_id"]):
            logger.info(f"[login:{self.sid}] localStorage 未取全,回退评论页拦截 post_list")
            try:
                await self.page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=8000)
                for _ in range(30):
                    if self.captured["aid"] and self.captured["finder_id"]:
                        break
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[login:{self.sid}] 回退跳转评论页失败: {e}")
        # 4. 登录微信:调 auth/auth_data API 取 userAttr.nickname(API 比 DOM 稳)
        wx_name = await self._fetch_wx_name()
        if wx_name:
            self.captured["wx_name"] = wx_name
        # 5. fallback:没抓到名字 -> 跳 /platform 首页
        if not dom_name:
            try:
                await self.page.goto("https://channels.weixin.qq.com/platform",
                                     wait_until="domcontentloaded", timeout=8000)
                await self.page.wait_for_timeout(3000)
                dom_name = await self._capture_name()
                if dom_name:
                    self.captured["name"] = dom_name
            except Exception as e:
                logger.warning(f"[login:{self.sid}] 跳 /platform 抓账号名失败: {e}")
        cur = self.captured.get("name", "")
        if not cur or cur == "未命名" or re.match(r"^(v2_|\d+$)", cur):
            self.captured["name"] = cur or self.captured["finder_id"][:12] or "未命名"

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

    async def _fetch_wx_name(self):
        """调 auth/auth_data(POST 空 body)取登录微信昵称(data.userAttr.nickname)。"""
        aid = self.captured.get("aid") or ""
        if not aid:
            return ""
        try:
            txt = await self.page.evaluate("""async (aid) => {
                const url = `https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data?_aid=${aid}&_pageUrl=${encodeURIComponent('https://channels.weixin.qq.com/micro/interaction/comment')}`;
                const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}', credentials:'include' });
                return await r.text();
            }""", aid)
            data = json.loads(txt)
            return ((data.get("data") or {}).get("userAttr") or {}).get("nickname") or ""
        except Exception as e:
            logger.debug(f"[login:{self.sid}] auth_data 取微信名失败: {e}")
            return ""

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
            acc["_wx_name"] = self.captured.get("wx_name") or acc.get("_wx_name", "")
            self._save_config()
            self._finalized = True
            self._finished("finalized")
            logger.info(f"[login:{self.sid}] 账号已更新(relogin): {acc['id']} ({acc['name']})")
            return acc
        # 排重:扫到的 _log_finder_id 已存在 -> 更新该已有账号(等同 relogin),不新增。
        # 用 _log_finder_id 而非 _aid:_aid 每次登录会变(同账号不同 _aid,非唯一),
        # _log_finder_id 才是账号稳定唯一标识。
        fid = self.captured["finder_id"]
        dup = next((a for a in self.config.get("accounts", [])
                    if a.get("_log_finder_id") and a["_log_finder_id"] == fid), None)
        if dup:
            # 把本次扫码的临时 profile 落到原账号 profile_dir(替换失效 cookie)
            target_dir = dup.get("profile_dir") or f"./profiles/{dup['id']}"
            final_dir = self._move_profile_to(target_dir)
            dup["_aid"] = self.captured["aid"] or dup.get("_aid", "")
            dup["_log_finder_id"] = fid
            dup["name"] = (name or self.captured["name"] or dup.get("name") or dup["id"]).strip()
            dup["_wx_name"] = self.captured.get("wx_name") or dup.get("_wx_name", "")
            dup["profile_dir"] = final_dir
            self._save_config()
            self._finalized = True
            self._finished("finalized")
            logger.info(f"[login:{self.sid}] 扫到已存在账号(_log_finder_id={fid[:20]}),已更新: {dup['id']} ({dup['name']})")
            # 返回副本带标记通知前端重启;标记不写回 config(dup 仍是 config 内对象)
            return {**dup, "_dedup_updated": True}
        acc_id = (account_id or self._gen_id()).strip() or self._gen_id()
        # 确保 id 唯一:与已有账号碰撞时加后缀(防删除账号后 _gen_id 序号回退重复 / 前端传重复 id)
        existing_ids = {a["id"] for a in self.config.get("accounts", [])}
        if acc_id in existing_ids:
            acc_id = f"{acc_id}_{self.sid[:4]}"
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
            "_wx_name": self.captured.get("wx_name", ""),
            "profile_dir": final_dir,
            "auto_comment_enabled": False,
            "auto_comment_content": "",
        }
        self.config.setdefault("accounts", []).append(acc)
        self._save_config()
        self._finalized = True
        self._finished("finalized")
        logger.info(f"[login:{self.sid}] 账号已保存: {acc_id} ({acc['name']})")
        return acc

    def _gen_id(self):
        existing = {a["id"] for a in self.config.get("accounts", [])}
        base = self.captured.get("name") or "acc"
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", base).lower()[:12] or "acc"
        n = len(self.config.get("accounts", [])) + 1
        acc_id = f"{slug}{n}"
        while acc_id in existing:  # 删除账号后序号回退可能碰撞,递增到唯一
            n += 1
            acc_id = f"{slug}{n}"
        return acc_id

    def _move_profile_to(self, target_dir):
        """把扫码临时 profile 迁移到 target_dir(替换失效 cookie)。返回最终 profile_dir;
        迁移失败(如原 profile 被运行中 worker 占用)则降级返回临时 dir,不抛错。"""
        if not self.profile_dir or not os.path.exists(self.profile_dir):
            return target_dir
        if os.path.abspath(self.profile_dir) == os.path.abspath(target_dir):
            return target_dir
        try:
            import shutil
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(self.profile_dir, target_dir)
            self.profile_dir = target_dir
            return target_dir
        except Exception as e:
            logger.warning(f"[login:{self.sid}] profile 迁移到 {target_dir} 失败,降级用临时 dir: {e}")
            return self.profile_dir

    def _save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
