"""多账号管理 + 扫码登录会话。

- AccountWorker:每账号独立 Playwright 持久化 profile + 评论页 + 并行轮询
- AccountManager:管理 workers + 登录会话 + 自动评论热更新 + 状态上报
- 登录态检测:未登录跳登录页;LoginSession 负责扫码 + 字段抓取(见 login_capture.py)
"""
import os
import json
import time
import random
import asyncio
from datetime import datetime
import logging
logger = logging.getLogger("sphgj")
from playwright.async_api import async_playwright
from .api_client import WxApiClient
from .comment_fetcher import CommentFetcher
from .auto_reply import AutoReply
from .auto_delete import AutoDelete
from .live_fetcher import LiveFetcher
from .auto_comment import AutoCommenter
from .login_capture import LoginSession
from .browser import launch_stealth, close_context_safely
from .selectors import COMMENT_URL, POST_CREATE_URL


def _in_night_hours(night_hours):
    """当前本地时是否在夜间时段内。night_hours=[start,end),支持跨天如 [22,6]。"""
    try:
        h = datetime.now().hour
        start, end = int(night_hours[0]), int(night_hours[1])
    except Exception:
        return False
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


class AccountWorker:
    def __init__(self, account, storage, config, emit=None):
        self.account = account
        self.storage = storage
        self.config = config
        self.emit = emit  # async emit(event, payload)
        self.api = None
        self.fetcher = None
        self.auto_reply = None
        self.auto_commenter = None
        self.auto_delete = None
        self.live_page = None
        self.live_fetcher = None
        self._live_loop_task = None
        self._live_probe_task = None     # 手动启动后短期开播探测任务
        self._live_opened_ts = 0.0   # live_page 开启时间(P0-1 按需开关)
        self._live_closed_ts = 0.0   # live_page 关闭时间
        self.live_info = None
        # 增值统计:每窗口独立采样(10分钟/30分钟各一套),增值=本次采样-上次采样
        self._ts_10m = 0.0
        self._last_a10 = None
        self._last_g10 = None
        self._audience_10m = 0
        self._gmv_10m = 0
        self._ts_30m = 0.0
        self._last_g30 = None
        self._gmv_30m = 0
        self.page = None
        self.context = None
        self._loop_task = None
        self._running = False
        self.logged_in = False
        self.last_scan_at = None
        self.new_count = 0
        self._new_count_date = None  # 当日新增计数日期(跨天重置 new_count)
        self._last_comment_reload = 0.0  # C: 评论页上次 reload 时间(每 2h reload 释放 JS 堆)
        # —— 按需浏览器生命周期(省内存) ——
        self._pw = None                      # playwright 实例(start 时注入)
        self._headless = True
        self._browser_lock = asyncio.Lock()  # 本账号浏览器引用计数锁
        self._browser_refs = 0               # 浏览器引用计数(抓取/手动操作/直播各持有一份)
        self._cycle_offset = 0.0             # 抓取周期错峰偏移(manager 按账号序号分配)
        self._fetch_lock = None              # 全局抓取串行锁(manager 注入,间隔排队)
        # 手动操作(回复/删除/置顶)延迟回收:操作后保持浏览器,120s 无新操作才回收
        # 避免连续手动操作反复开/关 Chromium 进程(省启动开销 + 更跟手)
        self._manual_hold = False            # 手动操作期间保持浏览器(标志,避免重复开)
        self._manual_release_task = None     # 手动操作后延迟回收计时器
        # 手动启动后的"开播探测窗口":此窗口内高频查直播,尽快捕获"启动后开播"。
        # 仅手动 start_account 触发(用户主动操作,短期开进程可接受);到期未播收回进程,
        # 交回 10 分钟抓取周期兜底。可在 config.live_probe_window_sec 调整(秒)。
        self._probe_window = float((self.config or {}).get("live_probe_window_sec", 180))
        # 直播信号丢失自恢复:重载 live_page 重试捕获(避免账号实际在播却因捕获瞬断被误关,需手动重启)
        self._live_nosignal_reloads = 0
        self._live_last_reload_attempt = 0.0

    async def start(self, playwright, headless=True):
        self._pw = playwright
        self._headless = headless
        os.makedirs(self.account["profile_dir"], exist_ok=True)
        # 恢复各窗口采样状态(重启 exe 后增值仍有效)
        try:
            st = self.storage.load_window_states(self.account["id"])
            w10 = st.get("10m")
            if w10:
                self._ts_10m = w10["ts"]; self._last_a10 = w10["audience"]
                self._last_g10 = w10["gmv"]; self._audience_10m = w10["delta_a"] or 0
                self._gmv_10m = w10["delta_g"] or 0
            w30 = st.get("30m")
            if w30:
                self._ts_30m = w30["ts"]; self._last_g30 = w30["gmv"]
                self._gmv_30m = w30["delta_g"] or 0
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 加载直播窗口状态失败: {e}")
        # 自动回复/评论/删除/抓取器:先建(内部持有 api 引用),开浏览器后统一重绑
        self.auto_reply = AutoReply(None, self.storage, self.account["id"],
                                    self.config.get("auto_reply") or {})
        self.auto_commenter = AutoCommenter(None, self.storage, self.account)
        self.auto_delete = AutoDelete(None, self.storage, self.account["id"],
                                      self.config.get("auto_delete") or {})
        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete)
        # 按需开浏览器(懒加载):不长期常驻 Chromium 进程——抓取/直播/手动操作时才开,
        # 空闲(非直播)即收回省内存。失败(如 profile 锁被占用)由调用方处理。
        try:
            await self._open_context()
        except Exception as e:
            logger.error(f"[{self.account['id']}] 启动开浏览器失败: {e}")
        self._running = True
        # 启动后做一次直播检测(仅检测:直播则保持,非直播则收回进程)
        if self.logged_in:
            await self._initial_live_check()

    # ======================== 按需浏览器生命周期(省内存) ========================

    async def _open_context(self):
        """懒加载开 Chromium:建 context+评论页,登录校验,建/重绑 API 客户端。幂等。"""
        if self.context is not None:
            return
        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)
        self.logged_in = "interaction/comment" in (self.page.url or "")
        if self.logged_in:
            logger.info(f"[{self.account['id']}] 已登录")
        else:
            logger.warning(f"[{self.account['id']}] 未登录(当前:{self.page.url}),请重新扫码")
        # _aid 每次登录会变,实时从 profile localStorage 读当前会话 _aid/_log_finder_id
        try:
            ls = await self.page.evaluate("""() => {
                const aid = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
                const fid = localStorage.getItem('finder_username') || '';
                let a = aid;
                try { a = JSON.parse(aid); } catch(e) { a = aid.replace(/^"|"$/g, ''); }
                return { aid: a, finder_id: fid };
            }""")
            if ls.get("aid"):
                self.account["_aid"] = ls["aid"]
            if ls.get("finder_id"):
                self.account["_log_finder_id"] = ls["finder_id"]
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 读 localStorage _aid 失败,用 config 旧值: {e}")
        self.api = WxApiClient(self.page, self.account, self.config, self.storage)
        # 重绑:自动回复/评论/删除/抓取器都持有 api 引用,重开浏览器后统一指向新客户端
        if self.auto_reply is not None: self.auto_reply.api = self.api
        if self.auto_commenter is not None: self.auto_commenter.api = self.api
        if self.auto_delete is not None: self.auto_delete.api = self.api
        if self.fetcher is not None: self.fetcher.api = self.api
        try:
            wxn = await self.api.fetch_wx_name()
            if wxn:
                self.account["_wx_name"] = wxn
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 取登录微信名失败: {e}")

    async def _close_context(self):
        """关 context 收回 Chromium 进程(兜底 kill 残留 chrome)。清所有页面引用。"""
        ctx = self.context
        lf = self.live_fetcher
        self.context = None
        self.page = None
        self.live_page = None
        self.live_fetcher = None
        if lf:
            try: await lf.close()
            except Exception: pass
        if ctx:
            await close_context_safely(ctx, self.account.get("profile_dir"), f"[{self.account['id']}] 收回进程")
            logger.info(f"[{self.account['id']}] 收回浏览器进程(省内存)")

    async def ensure_browser(self):
        """获取一次浏览器引用(引用计数)。返回 context 是否可用/已登录。"""
        async with self._browser_lock:
            self._browser_refs += 1
            if self.context is None:
                try:
                    await self._open_context()
                except Exception as e:
                    self._browser_refs = max(0, self._browser_refs - 1)
                    logger.error(f"[{self.account['id']}] 开浏览器失败: {e}")
                    return False
            return self.logged_in

    async def release_idle_browser(self):
        """释放一次浏览器引用;引用归零且非直播时关 context 收回进程。"""
        async with self._browser_lock:
            self._browser_refs = max(0, self._browser_refs - 1)
            if self._browser_refs > 0:
                return
            if self.live_fetcher is not None:
                return  # 直播中保持常开
            if self.context is not None:
                await self._close_context()

    async def hold_for_manual(self, delay=None):
        """手动操作(回复/删除/置顶)期间保持浏览器,操作完成后延迟回收省内存。

        - 首次调用:经引用计数开浏览器(只 +1),开进程;
        - delay 内再次调用(连回复数条):_manual_hold 已置位,不再重复开进程,只续期计时;
        - 计时到期:释放这一份引用并回收(非直播且无其它引用时关 context)。
        与抓取/直播的引用计数相互独立(同一把 _browser_lock 串行),互不干扰。

        delay 默认 120s,可经 config.manual_release_delay_sec 调整。
        """
        if delay is None:
            delay = float((self.config or {}).get("manual_release_delay_sec", 120))
        async with self._browser_lock:
            if not self._manual_hold:
                self._browser_refs += 1
                if self.context is None:
                    try:
                        await self._open_context()
                    except Exception as e:
                        self._browser_refs = max(0, self._browser_refs - 1)
                        logger.error(f"[{self.account['id']}] 手动操作开浏览器失败: {e}")
                        return False
                self._manual_hold = True
            # 已 hold:ref 已在首次占住,不重复 +1,仅续期计时
        # 续期/新建延迟回收计时器(取消旧的,启新的)
        if self._manual_release_task is not None and not self._manual_release_task.done():
            self._manual_release_task.cancel()
        self._manual_release_task = asyncio.create_task(self._delayed_close_manual(delay))
        return self.logged_in

    async def _delayed_close_manual(self, delay):
        """延迟回收:到点释放手动持有的那一份引用;非直播且无其它引用时关 context。

        与 hold_for_manual 的 +1 在 _browser_lock 内配对,避免竞态下重复关/漏关。
        """
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._browser_lock:
            self._manual_hold = False
            self._browser_refs = max(0, self._browser_refs - 1)
            if self._browser_refs == 0 and self.live_fetcher is None and self.context is not None:
                await self._close_context()

    async def _open_and_detect_live(self):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。

        关键修复: page.on('response') 是【被动】捕获,响应在 page 加载后由页面 JS 异步发出,
        goto 完成瞬间 check_live_status 尚未返回。若 goto 后只取一次就判定,会因 live_stats
        仍为空误判"非直播"而立即关掉 live_page —— 这正是"直播大屏视频流消失"的根因
        (live_page 还没拿到 stream_url 就被关了)。故 goto 后必须【等响应到达】再判定。

        返回 is_live(bool): True 时保留 live_page+live_fetcher 并启动 _live_loop 流式推送;
        False 时关掉 live_page 释放(后续交由 10 分钟抓取周期 / 开播探测窗口再判定)。
        """
        if self.live_fetcher is not None:
            return True
        try:
            self.live_page = await self.context.new_page()
            # 用局部 lf 持有 LiveFetcher,避免并发 _close_context/release_idle_browser 将
            # self.live_fetcher 置 None 后,本方法内再访问 .updated_at_ts 触发 'NoneType' 崩溃。
            lf = LiveFetcher(self.live_page, self.account["id"])
            self.live_fetcher = lf
            await lf.goto_live()
            self._live_opened_ts = time.time()
            self._live_nosignal_reloads = 0
            info = await self._wait_for_live_detection(lf, timeout=20.0)
            # 判定:有 live_stats(近期)即直播;stream_url / live_object_id 强信号也视为直播(视频流必出)
            last_ts = lf.updated_at_ts or 0
            fresh = (time.time() - last_ts) < 30
            is_live = bool(info.get("live_stats") and fresh)
            if not is_live and (lf.stream_url or lf.live_object_id):
                is_live = True
                logger.info(f"[{self.account['id']}] 检测到直播信号(stream_url/live_object_id 已取到),开始流式推送")
            if is_live:
                self.start_live_loop()
                logger.info(f"[{self.account['id']}] 检测到直播,开始流式推送"
                            f"(live_object_id={lf.live_object_id}, "
                            f"stream_url={'有' if lf.stream_url else '待取'})")
                return True
            # 非直播:关页回收(检测窗口内未拿到任何直播信号)
            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(live_stats={info.get('live_stats') is not None}, "
                        f"live_object_id={lf.live_object_id})")
            try: await lf.close()
            except Exception: pass
            try: await self.live_page.close()
            except Exception: pass
            self.live_page = None
            self.live_fetcher = None
            self._live_closed_ts = time.time()
            return False
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 直播检测失败: {e}")
            lf = self.live_fetcher  # 可能已被并发置 None,下面统一判空处理
            if lf:
                try: await lf.close()
                except Exception: pass
            if self.live_page:
                try: await self.live_page.close()
                except Exception: pass
            self.live_page = None
            self.live_fetcher = None
            self._live_closed_ts = time.time()
            return False

    async def _wait_for_live_detection(self, lf, timeout=20.0):
        """goto 之后被动等待 check_live_status/get_live_info 响应到达(最多 timeout 秒)。

        用 snapshot()(不告警)轮询,避免检测阶段误触发"stream_url 为空"告警;
        一旦取到 live_stats / live_object_id / stream_url 任意一项即认为响应已到,立即返回。
        """
        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            info = await lf.snapshot()
            if info.get("live_stats") or lf.live_object_id or lf.stream_url:
                return info
            await asyncio.sleep(2)
        return info or {}

    async def _initial_live_check(self):
        """启动后即时直播检测:开 live_page 等响应判定;直播则保持并流式推送,
        非直播则关页,随后 release_idle_browser 收回评论页进程(空闲不占内存)。"""
        if not self.logged_in or self.context is None:
            return
        await self._open_and_detect_live()
        # 非直播:收回评论页进程;直播:保持(live_fetcher 存在)
        await self.release_idle_browser()

    async def _cycle_live_check(self):
        """评论抓取周期内顺带直播检测(跟随 10 分钟周期)。

        - 已在直播(live_fetcher 存在):交给 _live_loop 处理,跳过。
        - 未在直播:开 live_page 等响应判定;直播则保持并启动 _live_loop,非直播则关页
          (随后 release_idle_browser 收回进程)。
        这样非直播账号的直播检测跟随 10 分钟抓取周期,无需常驻浏览器轮询。
        """
        if not self.logged_in:
            return
        if self.live_fetcher is not None:
            return
        await self._open_and_detect_live()

    def start_loop(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._fetch_loop())

    async def _fetch_loop(self):
        """评论抓取循环(按需开/关浏览器以省内存)。

        - 每 fetch_interval_sec(默认 600s = 10 分钟)抓一次;多账号经 _cycle_offset 错峰
          + 全局 _fetch_lock 串行,形成"间隔排队":任意时刻最多 1 个账号在抓,
          浏览器不并发常驻。
        - 抓取用 ensure_browser 开浏览器,结束 release_idle_browser 收回(非直播账号
          抓完即关 Chromium 进程,空闲时不占内存)。
        """
        interval = self.config.get("fetch_interval_sec", 600)
        rc = (self.config.get("risk_control") or {})
        night_hours = rc.get("night_hours", [0, 6])
        night_mult = rc.get("night_interval_multiplier", 3)
        # 错峰:首个账号 offset 由 manager 分配(均匀错开 0~interval),避免同时拉起浏览器
        await asyncio.sleep(self._cycle_offset)
        while self._running:
            # 串行抓取:全局队列锁,保证多账号不同时拉起浏览器(省内存 + 防风控关联)
            if self._fetch_lock is not None:
                await self._fetch_lock.acquire()
            try:
                ok = await self.ensure_browser()
                if not ok:
                    logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
                    await asyncio.sleep(min(interval, 60))
                    continue
                if self.logged_in and self.fetcher:
                    try:
                        scanned, new_c, new_comments, deleted_ids = await self.fetcher.fetch_all()
                        self.last_scan_at = datetime.now().isoformat()
                        if new_c:
                            today = datetime.now().strftime("%Y-%m-%d")
                            if self._new_count_date != today:
                                self._new_count_date = today
                                self.new_count = 0
                            self.new_count += new_c
                        logger.info(f"[{self.account['id']}] 扫描{scanned}视频 新增{new_c}评论")
                        if new_comments:
                            await self._emit("comments_update",
                                             {"account_id": self.account["id"], "comments": new_comments})
                        for cid in deleted_ids:
                            await self._emit("comment_deleted", {"comment_id": cid})
                    except Exception as e:
                        logger.error(f"[{self.account['id']}] 抓取异常: {e}")
                # 周期内顺带检测直播(非直播则收回进程)
                await self._cycle_live_check()
            finally:
                if self._fetch_lock is not None:
                    try: self._fetch_lock.release()
                    except Exception: pass
                # 无动作(非直播/无手动操作)则收回浏览器进程省内存
                await self.release_idle_browser()
            # 夜间降频(0~6 点间隔拉长)+ 抖动
            cur = interval
            if _in_night_hours(night_hours):
                cur = int(interval * night_mult)
            await asyncio.sleep(cur + random.uniform(0, min(cur, 30)))

    async def fetch_once(self):
        ok = await self.ensure_browser()
        try:
            if ok and self.logged_in and self.fetcher:
                return await self.fetcher.fetch_all()
        finally:
            await self.release_idle_browser()
        return 0, 0, [], []

    def start_live_loop(self):
        if self._live_loop_task is None or self._live_loop_task.done():
            self._live_loop_task = asyncio.create_task(self._live_loop())

    def start_live_probe(self):
        """手动启动后短期开播探测:高频查直播,尽快捕获"启动后开播"。

        仅手动 start_account 调用(用户主动操作,短期开进程可接受)。复用 _cycle_live_check
        (已有的直播检测逻辑),窗口内每 ~24s 查一次,检测到直播则转 _live_loop 流式;
        窗口结束仍未播则收回进程,交回 10 分钟抓取周期兜底。
        """
        if self._live_probe_task is None or self._live_probe_task.done():
            self._live_probe_task = asyncio.create_task(self._live_probe_loop())

    async def _live_probe_loop(self):
        probe_until = time.time() + self._probe_window
        interval = self.config.get("live_check_interval_sec", 8)
        ok = await self.ensure_browser()
        if not ok:
            return
        try:
            while self._running and time.time() < probe_until:
                if self.logged_in:
                    # 借用全局抓取锁,避免与 10 分钟抓取并发拉起浏览器
                    if self._fetch_lock is not None:
                        await self._fetch_lock.acquire()
                    try:
                        await self._cycle_live_check()
                    finally:
                        if self._fetch_lock is not None:
                            try: self._fetch_lock.release()
                            except Exception: pass
                    if self.live_fetcher is not None:
                        logger.info(f"[{self.account['id']}] 开播探测窗口内检测到直播,转流式推送")
                        return
                await asyncio.sleep(interval * 3)
        finally:
            # 未播:收回进程(live_fetcher 为 None 时 release 会关 context);
            # 已播:live_fetcher 非 None,release 提前返回不关,保持流式。
            await self.release_idle_browser()

    async def _live_loop(self):
        while self._running:
            interval = self.config.get("live_check_interval_sec", 8)
            dash_interval = self.config.get("dashboard_interval_sec", 60)
            # 非直播:本循环不持有浏览器,交由 _cycle_live_check(跟随评论抓取周期,每
            # fetch_interval_sec 一次)检测开播。轻量睡眠避免空转占 CPU。
            if self.live_fetcher is None:
                await asyncio.sleep(interval * 3)
                continue
            # 用局部 lf 持有 LiveFetcher,避免并发 _close_context/release_idle_browser 将
            # self.live_fetcher 置 None 后,本方法内再访问 .updated_at_ts 触发 'NoneType' 崩溃。
            lf = self.live_fetcher
            if self.logged_in and self.context:
                now = time.time()
                # 每 ~48 分钟 close+重建 live_page(替代 reload):reload 不退出 Chromium renderer,
                # 直播期间 FLV 流涨到的峰值内存不归还 OS;close page 触发 renderer 退出归还内存,
                # 新 page 起新 renderer 重新加载,长跑内存增长比 reload 彻底。
                self._last_live_reload = getattr(self, "_last_live_reload", 0.0)
                if self.live_page and now - self._last_live_reload >= 2880:
                    rebuilt = False
                    try:
                        if lf:
                            await lf.close()  # P0: 清回调+挂起 task
                        await self.live_page.close()         # 关 page 促 renderer 退出
                        self.live_page = await self.context.new_page()
                        lf = LiveFetcher(self.live_page, self.account["id"])
                        self.live_fetcher = lf
                        await lf.goto_live()
                        self._last_live_reload = now
                        rebuilt = True
                        logger.info(f"[{self.account['id']}] live_page 定期 close+重建(释放 renderer 内存)")
                    except Exception as e:
                        logger.warning(f"[{self.account['id']}] live_page close+重建失败,降级 reload: {e}")
                        try:
                            if self.live_page:
                                await self.live_page.reload(wait_until="domcontentloaded")
                                self._last_live_reload = now
                                rebuilt = True
                        except Exception as e2:
                            logger.debug(f"[{self.account['id']}] live_page reload 兜底也失败: {e2}")
                    if not rebuilt:
                        # 都失败:清空引用,下一轮走重开分支重建
                        self.live_page = None
                        self.live_fetcher = None
                        self._live_closed_ts = now
                try:
                    info = await lf.fetch()
                    self.live_info = info
                    # 增值统计:采样累计观看/GMV,算 10分钟观看、10/30分钟 GMV 增值
                    stats = info.get("live_stats") or {}
                    audience = stats.get("totalAudienceCount")
                    gmv = stats.get("payedGmv")
                    if audience is not None or gmv is not None:
                        # 首次有数据:存基准(开播时),暂不算增值
                        if self._ts_10m == 0:
                            self._ts_10m = now
                            self._last_a10 = audience
                            self._last_g10 = gmv
                            self._persist_window("10m", now, audience, gmv, 0, 0)
                        if self._ts_30m == 0:
                            self._ts_30m = now
                            self._last_g30 = gmv
                            self._persist_window("30m", now, audience, gmv, 0, 0)
                        # 10 分钟采样:增值 = 本次 - 上次(10 分钟前),精确 10 分钟窗口
                        if now - self._ts_10m >= 600:
                            self._audience_10m = self._delta(audience, self._last_a10)
                            self._gmv_10m = self._delta(gmv, self._last_g10)
                            self._last_a10 = audience
                            self._last_g10 = gmv
                            self._ts_10m = now
                            self._persist_window("10m", now, audience, gmv,
                                                 self._audience_10m, self._gmv_10m)
                        # 30 分钟采样:增值 = 本次 - 上次(30 分钟前),精确 30 分钟窗口
                        if now - self._ts_30m >= 1800:
                            self._gmv_30m = self._delta(gmv, self._last_g30)
                            self._last_g30 = gmv
                            self._ts_30m = now
                            self._persist_window("30m", now, audience, gmv, 0, self._gmv_30m)
                    # is_live: 有 live_stats 且 get_live_info 近期仍返回数据(30秒内)
                    last_ts = lf.updated_at_ts or 0
                    info["is_live"] = bool(info.get("live_stats") and (now - last_ts < 30))
                    # 自恢复:账号实际可能在播,但 check_live_status 捕获偶发瞬断(长驻页面常见)。
                    # 信号丢失 >120s 不直接关页,而是重载 live_page 重触发捕获(最多 3 次,间隔 60s),
                    # 成功拿到信号则继续流式;彻底失败(3 次仍无)才判定下播关页回收。
                    if not info["is_live"]:
                        idle = (now - self._live_opened_ts) if last_ts == 0 else (now - last_ts)
                        page_alive = bool(self.live_page) and "liveBuild" in (self.live_page.url or "")
                        can_reload = page_alive and (self._live_nosignal_reloads or 0) < 3 \
                            and (now - (self._live_last_reload_attempt or 0)) > 60
                        if idle > 120 and can_reload:
                            self._live_nosignal_reloads = (self._live_nosignal_reloads or 0) + 1
                            self._live_last_reload_attempt = now
                            try:
                                if lf:
                                    await lf.close()
                                await self.live_page.reload(wait_until="domcontentloaded")
                                lf = LiveFetcher(self.live_page, self.account["id"])
                                self.live_fetcher = lf
                                await lf.goto_live()
                                self._last_live_reload = now
                                logger.info(f"[{self.account['id']}] 直播信号丢失,重载 live_page 重试捕获"
                                            f"(第{self._live_nosignal_reloads}次/最多3次)")
                                await asyncio.sleep(interval * 3)
                                continue
                            except Exception as e:
                                logger.warning(f"[{self.account['id']}] 信号丢失重载失败: {e}")
                        if idle > 120:
                            # P2: 关 live_page 前先推"已下播"(stream_url=None/is_live=False),
                            # 让前端 LiveScreenPage 销毁 flv.js player 释放 MSE 缓冲。
                            # 否则前端 state 冻结在下播前的 stream_url,player 不 destroy,4 账号全残留。
                            try:
                                await self._emit("live_screen_update", {
                                    "account_id": self.account["id"],
                                    "name": self.account.get("name", self.account["id"]),
                                    "live_stats": None, "stream_url": None,
                                    "updated_at": None, "is_live": False, "metrics": {},
                                })
                            except Exception: pass
                            # P0: 先清理 LiveFetcher 回调+挂起 task,再关 page,防泄漏
                            if lf:
                                try: await lf.close()
                                except Exception: pass
                            try: await self.live_page.close()
                            except Exception: pass
                            self.live_page = None
                            self.live_fetcher = None
                            self._live_closed_ts = now
                            self._live_nosignal_reloads = 0
                            logger.info(f"[{self.account['id']}] 非直播,关闭 live_page 省内存(idle={int(idle)}s)")
                            # 收回评论页进程(非直播且无其它引用时)
                            await self.release_idle_browser()
                            await asyncio.sleep(interval)
                            continue
                    # 成功拿到直播信号:重置自愈重试计数,便于下次真正瞬断时重新自愈
                    self._live_nosignal_reloads = 0
                    # dashboardV4 指标,按 config.dashboard_interval_sec 抓(需 live_object_id,账号在直播)
                    if lf.live_object_id and now - (lf._dashboard_ts or 0) >= dash_interval:
                        try:
                            dd = await lf.fetch_dashboard_data()
                            if dd:
                                info["metrics"] = dd
                        except Exception as e:
                            logger.debug(f"[{self.account['id']}] dashboard 抓取失败: {e}")
                    # 增值统计 + 直播时长(本地,非 dashboard API)并入 metrics,供卡片渲染(随 metrics 下发,status 路由也有)
                    m = dict(info.get("metrics") or {})
                    m["audience_10m"] = self._audience_10m
                    m["gmv_10m"] = self._gmv_10m
                    m["gmv_30m"] = self._gmv_30m
                    m["liveDuration"] = (info.get("live_stats") or {}).get("liveDurationInSeconds")
                    info["metrics"] = m
                    await self._emit("live_screen_update", {
                        "account_id": self.account["id"],
                        "name": self.account.get("name", self.account["id"]),
                        **info,
                    })
                except Exception as e:
                    logger.error(f"[{self.account['id']}] 直播抓取异常: {e}")
            await asyncio.sleep(interval)

    def _persist_window(self, window, ts, audience, gmv, delta_a, delta_g):
        try:
            self.storage.save_window_state(self.account["id"], window, ts, audience, gmv, delta_a, delta_g)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 窗口状态持久化失败: {e}")

    def _delta(self, current, past):
        """current - past;current 非数字或无基准时返回 0。"""
        if not isinstance(current, (int, float)):
            return 0
        if not isinstance(past, (int, float)):
            return 0
        return current - past

    async def _emit(self, event, payload):
        if self.emit:
            try:
                await self.emit(event, payload)
            except Exception as e:
                logger.debug(f"emit 失败 {event}: {e}")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._live_loop_task:
            self._live_loop_task.cancel()
            try:
                await self._live_loop_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._live_probe_task:
            self._live_probe_task.cancel()
            try:
                await self._live_probe_task
            except (asyncio.CancelledError, Exception):
                pass
        # 取消手动操作延迟回收计时器,避免 worker 停后它仍试图关 context
        if self._manual_release_task is not None and not self._manual_release_task.done():
            self._manual_release_task.cancel()
            try:
                await self._manual_release_task
            except (asyncio.CancelledError, Exception):
                pass
        self._manual_release_task = None
        self._manual_hold = False
        # P0: 关 context 前先清理 live_fetcher 回调+挂起 task,防泄漏
        if self.live_fetcher:
            try: await self.live_fetcher.close()
            except Exception: pass
            self.live_fetcher = None
        # context.close 在关闭带 FLV 直播流的 context 时可能抛
        # "Browser has been closed"/管道异常;用 close_context_safely 吞掉并
        # 按 profile_dir 兜底 kill 残留 chrome 进程,清空引用。
        # 否则异常上抛会中断 stop_all,后续 worker 的浏览器不被关闭,
        # 进程退出时仍在推直播事件 -> 驱动写已断管道 EPIPE 崩溃;且孤儿进程堆积。
        if self.context:
            await close_context_safely(self.context, self.account.get("profile_dir"),
                                       f"[{self.account['id']}]")
            self.context = None
            self.page = None
            self.live_page = None


class AccountManager:
    def __init__(self, config, storage, emit=None):
        self.config = config
        self.storage = storage
        self.emit = emit  # async emit(event, payload)
        self.workers = {}          # account_id -> AccountWorker
        self.login_sessions = {}   # sid -> LoginSession
        self._playwright = None
        self._running = False
        self._fetch_lock = None          # 评论抓取全局串行锁(间隔排队)
        self._relogin_queue = []         # 待自动 relogin 的 account_id
        self._relogin_active_acc = None  # 当前正在 auto-relogin 的 account_id
        self._relogin_active_sid = None
        self._open_browsers = {}         # account_id -> headed BrowserContext(用户手动操作)

    async def _ensure_playwright(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _emit(self, event, payload):
        if self.emit:
            try:
                await self.emit(event, payload)
            except Exception as e:
                logger.debug(f"emit 失败 {event}: {e}")

    # ---------- 扫码登录 ----------
    async def start_login(self, headed=False):
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit)
        self.login_sessions[sess.sid] = sess
        await sess.start(headed=headed)
        return sess

    def get_login_session(self, sid):
        return self.login_sessions.get(sid)

    async def open_window_login(self, sid):
        s = self.login_sessions.get(sid)
        if s:
            await s.open_window()
        return s

    async def cancel_login(self, sid):
        s = self.login_sessions.pop(sid, None)
        if s:
            await s.cancel()

    async def finalize_login(self, sid, account_id=None, name=None):
        s = self.login_sessions.pop(sid, None)
        if not s:
            return None
        # 排重预热:若扫到的 _log_finder_id 已存在且该账号 worker 在运行,先停掉释放 profile 锁,
        # 便于 finalize 把新 cookie 落到原 profile_dir(否则迁移会被占用而降级)。
        # 用 _log_finder_id 而非 _aid(_aid 每次登录变,不唯一)。
        fid = s.captured.get("finder_id")
        if fid:
            dup = next((a for a in self.config.get("accounts", []) if a.get("_log_finder_id") == fid), None)
            if dup and dup["id"] in self.workers:
                await self.stop_account(dup["id"])
        return await s.finalize_with_id(account_id, name)

    # ---------- 引擎 ----------
    async def start(self, headless=True):
        await self._ensure_playwright()
        self._running = True
        # 评论抓取全局串行锁:多账号经此锁 + _cycle_offset 错峰,形成"间隔排队",
        # 任意时刻最多 1 个账号在抓,浏览器不并发常驻(省内存)。
        self._fetch_lock = asyncio.Lock()
        interval = self.config.get("fetch_interval_sec", 600)
        total = max(1, len(self.config.get("accounts", [])))
        live_idx = 0
        expired = []
        for acc in self.config.get("accounts", []):
            try:
                w = await self.add_account(acc, headless=headless)
                if w and not w.logged_in:
                    # 未登录:立即关 context 释放进程(否则 chrome-headless-shell.exe 常驻堆积),
                    # 不入运行态,等自动 relogin 扫码后再开
                    await w.stop()
                    self.workers.pop(acc["id"], None)
                    expired.append(acc["id"])
                    continue
                if w and w.logged_in:
                    # 间隔排队:在 fetch_interval_sec 窗口内按账号序号均匀错峰
                    w._fetch_lock = self._fetch_lock
                    w._cycle_offset = live_idx * interval / total
                    live_idx += 1
            except Exception as e:
                logger.error(f"账号 {acc.get('id')} 启动失败: {e}")
        for w in self.workers.values():
            if w.logged_in:
                w.start_loop()
                w.start_live_loop()
        await self._emit("engine_status", self.status_snapshot())
        # 失效账号:后端自动依次弹 headed 扫码窗口,无需用户手动点启动
        for acc_id in expired:
            await self.enqueue_auto_relogin(acc_id)

    async def stop(self):
        self._running = False
        # 并发关闭各 worker:多账号串行关带 FLV 流的 context 很慢,
        # 易触发 graceful_shutdown 的 5s 超时;return_exceptions 保证
        # 单个 worker 失败不中断其余(每个 w.stop 内部已吞 context.close 异常)。
        workers = list(self.workers.values())
        if workers:
            await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
        self.workers.clear()
        await self._emit("engine_status", self.status_snapshot())

    async def add_account(self, account, headless=True):
        worker = AccountWorker(account, self.storage, self.config, self._emit)
        try:
            await worker.start(self._playwright, headless=headless)
        except Exception:
            # start 抛异常(launch_stealth 失败/goto 超时)时 context 可能已开,需清理避免进程残留
            await worker.stop()
            raise
        self.workers[account["id"]] = worker
        return worker

    async def start_account(self, account_id):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        await self._ensure_playwright()
        if account_id in self.workers:
            return self.workers[account_id]
        if self._fetch_lock is None:
            self._fetch_lock = asyncio.Lock()
        w = AccountWorker(acc, self.storage, self.config, self._emit)
        # 按账号在配置中的序号分配错峰偏移,与既有 worker 错开抓取节奏
        idx = next((i for i, a in enumerate(self.config.get("accounts", [])) if a["id"] == account_id), 0)
        total = max(1, len(self.config.get("accounts", [])))
        w._fetch_lock = self._fetch_lock
        w._cycle_offset = idx * self.config.get("fetch_interval_sec", 600) / total
        await w.start(self._playwright, headless=True)
        if w.logged_in:
            w.start_loop()
            w.start_live_loop()
            self.workers[account_id] = w
            # 手动启动:首检未播则进入短期开播探测(每 ~24s 查一次,最长 live_probe_window_sec 秒),
            # 尽快捕获"启动后开播"——解决非直播账号开播检测跟随 10 分钟抓取周期的延迟问题。
            if w.live_fetcher is None:
                w.start_live_probe()
        else:
            # 未登录:关掉刚开的 context 释放进程(避免 chrome-headless-shell.exe 残留),
            # 不入 workers;前端据返回的 logged_in=false 自动转 relogin 扫码。
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())
        return w

    async def start_relogin(self, account_id, auto=False, on_finished=None):
        """已存在账号重新扫码登录:复用原 profile_dir(headed),成功后更新原账号字段。

        auto=True 用于失效账号自动依次扫码:on_finished 在终态回调以推进队列。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        if account_id in self.workers:
            await self.stop_account(account_id)
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit, account=acc,
                            auto_finalize=auto, on_finished=on_finished)
        self.login_sessions[sess.sid] = sess
        await sess.start(headed=True)
        return sess

    # ---------- 失效账号自动依次扫码 ----------
    async def enqueue_auto_relogin(self, account_id):
        """失效账号入队自动 relogin(headed 弹窗依次扫码);无活跃项时立即弹下一个。"""
        if not account_id or account_id in self._relogin_queue or account_id == self._relogin_active_acc:
            return
        self._relogin_queue.append(account_id)
        await self._emit("relogin_queue", self._relogin_snapshot())
        if self._relogin_active_acc is None:
            await self._run_next_relogin()

    async def _run_next_relogin(self):
        if not self._relogin_queue:
            self._relogin_active_acc = None
            self._relogin_active_sid = None
            await self._emit("relogin_queue", self._relogin_snapshot())
            return
        acc_id = self._relogin_queue.pop(0)
        self._relogin_active_acc = acc_id
        self._relogin_active_sid = None
        await self._emit("relogin_queue", self._relogin_snapshot())

        def on_finished(sid, status, aid):
            # LoginSession 终态在事件循环线程触发,直接 create_task 推进
            asyncio.create_task(self._on_relogin_finished(aid, status))

        try:
            sess = await self.start_relogin(acc_id, auto=True, on_finished=on_finished)
            if sess:
                self._relogin_active_sid = sess.sid
            else:
                await self._on_relogin_finished(acc_id, "failed")
        except Exception as e:
            logger.error(f"自动 relogin 启动失败 {acc_id}: {e}")
            await self._on_relogin_finished(acc_id, "failed")

    async def _on_relogin_finished(self, account_id, status):
        if self._relogin_active_sid:
            self.login_sessions.pop(self._relogin_active_sid, None)
        if status == "finalized":
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"自动 relogin 后启动失败 {account_id}: {e}")
        self._relogin_active_acc = None
        self._relogin_active_sid = None
        await self._emit("relogin_queue", self._relogin_snapshot())
        await self._run_next_relogin()

    def _relogin_snapshot(self):
        active = self._relogin_active_acc
        active_name = None
        if active:
            acc = next((a for a in self.config.get("accounts", []) if a["id"] == active), None)
            active_name = acc.get("name") if acc else None
        return {"active": active, "active_name": active_name, "pending": list(self._relogin_queue)}

    async def stop_account(self, account_id):
        w = self.workers.pop(account_id, None)
        if w:
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())

    async def open_account_browser(self, account_id):
        """打开带登录态的 headed 浏览器供用户手动操作。
        同 profile_dir 不能与 worker 并存(Chromium profile 锁),先停 worker;
        用户关浏览器后自动重启 worker 恢复抓取。"""
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        if account_id in self._open_browsers:
            return True
        was_running = account_id in self.workers
        if was_running:
            await self.stop_account(account_id)
        pw = await self._ensure_playwright()
        try:
            ctx = await launch_stealth(pw, acc["profile_dir"], headless=False, window_size=(1920, 1035))
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(POST_CREATE_URL, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"[{account_id}] 打开浏览器 goto 失败(忽略): {e}")
            self._open_browsers[account_id] = ctx
            asyncio.create_task(self._hold_open_browser(account_id, was_running))
            logger.info(f"[{account_id}] 已打开 headed 浏览器(原{'运行中' if was_running else '未运行'})")
            return True
        except Exception as e:
            logger.error(f"[{account_id}] 打开浏览器失败: {e}")
            if was_running:
                try:
                    await self.start_account(account_id)
                except Exception:
                    pass
            return False

    async def open_dashboard(self, account_id):
        """打开当前直播的 dashboardV4 大屏(带 liveObjectId,需账号在直播)。

        同 open_account_browser:停 worker -> 开 headed(1920x1035) -> goto dashboardV4。
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
        was_running = account_id in self.workers
        if was_running:
            await self.stop_account(account_id)
        pw = await self._ensure_playwright()
        try:
            ctx = await launch_stealth(pw, acc["profile_dir"], headless=False, window_size=(1920, 1035))
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            url = f"https://channels.weixin.qq.com/platform/statistic/dashboardV4?objetctId={live_object_id}&entrance_id=2"
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"[{account_id}] 打开 dashboardV4 goto 失败(忽略): {e}")
            self._open_browsers[account_id] = ctx
            asyncio.create_task(self._hold_open_browser(account_id, was_running))
            logger.info(f"[{account_id}] 已打开 dashboardV4 大屏 liveObjectId={live_object_id}(原{'运行中' if was_running else '未运行'})")
            return True, ""
        except Exception as e:
            logger.error(f"[{account_id}] 打开 dashboardV4 失败: {e}")
            if was_running:
                try:
                    await self.start_account(account_id)
                except Exception:
                    pass
            return False, str(e)

    async def _hold_open_browser(self, account_id, was_running=True):
        """保持 headed 浏览器;用户关所有 page 后关 context,只在原本在跑时重启 worker。

        用 close_context_safely 兜底 kill 残留 chrome 进程(带 FLV 流的 context.close
        易抛异常致孤儿进程)。was_running=False(原本未启动)时关窗不自动启动,避免凭空
        给未启动账号开常驻进程。"""
        ctx = self._open_browsers.get(account_id)
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
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        profile_dir = acc["profile_dir"] if acc else None
        await close_context_safely(ctx, profile_dir, f"[{account_id}] headed")
        self._open_browsers.pop(account_id, None)
        if was_running:
            logger.info(f"[{account_id}] headed 浏览器已关闭,重启 worker")
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"[{account_id}] 浏览器关闭后重启 worker 失败: {e}")
        else:
            logger.info(f"[{account_id}] headed 浏览器已关闭(原未运行,不自动启动)")

    async def fetch_all_once(self):
        tasks = [w.fetch_once() for w in self.workers.values() if w.logged_in]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def get_worker(self, account_id):
        return self.workers.get(account_id)

    # ---------- 自动评论热更新(诉求2:账号管理模块内可随时改) ----------
    def update_auto_comment(self, account_id, enabled, content):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        acc["auto_comment_enabled"] = bool(enabled)
        acc["auto_comment_content"] = content or ""
        self._save_config()
        w = self.workers.get(account_id)
        if w and w.auto_commenter:
            w.auto_commenter.account["auto_comment_enabled"] = bool(enabled)
            w.auto_commenter.account["auto_comment_content"] = content or ""
        return True

    def update_account(self, account_id, name=None):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        if name is not None:
            acc["name"] = name
        self._save_config()
        return True

    def delete_account(self, account_id, remove_profile=False):
        before = len(self.config.get("accounts", []))
        self.config["accounts"] = [a for a in self.config.get("accounts", []) if a["id"] != account_id]
        if len(self.config["accounts"]) == before:
            return False
        self._save_config()
        if remove_profile:
            import shutil
            shutil.rmtree(f"./profiles/{account_id}", ignore_errors=True)
        return True

    def _save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    # ---------- 状态 ----------
    def status_snapshot(self):
        stats = {s["account_id"]: s for s in self.storage.account_stats()}
        return {
            "running": self._running,
            "accounts": [self._account_status(a, stats) for a in self.config.get("accounts", [])],
        }

    def _account_status(self, acc, stats):
        w = self.workers.get(acc["id"])
        st = stats.get(acc["id"], {})
        return {
            "id": acc["id"],
            "name": acc.get("name", acc["id"]),
            "logged_in": bool(w and w.logged_in),
            "running": bool(w and w._running),
            "last_scan": getattr(w, "last_scan_at", None) if w else None,
            "new_comments": getattr(w, "new_count", 0) if w else 0,
            "total_comments": st.get("total", 0),
            "replied": st.get("replied", 0),
            "last_fetched": st.get("last_fetched"),
            "auto_comment_enabled": bool(acc.get("auto_comment_enabled")),
            "auto_comment_content": acc.get("auto_comment_content", ""),
            "has_aid": bool(acc.get("_aid")),
            "wx_name": acc.get("_wx_name", ""),
        }

    async def stop_all(self):
        """进程退出前清理:停 workers + 登录会话 + playwright。

        每一步都吞异常:任一环节失败不能阻断后续清理,否则残留的活浏览器
        在进程退出时仍推事件 -> 驱动 EPIPE 崩溃。
        """
        await self.stop()
        for s in list(self.login_sessions.values()):
            try:
                await s.cancel()
            except Exception as e:
                logger.debug(f"login session cancel 异常(忽略): {e}")
        self.login_sessions.clear()
        for acc_id, ctx in list(self._open_browsers.items()):
            acc = next((a for a in self.config.get("accounts", []) if a["id"] == acc_id), None)
            await close_context_safely(ctx, acc["profile_dir"] if acc else None,
                                       f"[{acc_id}] headed")
        self._open_browsers.clear()
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.debug(f"playwright.stop 异常(忽略): {e}")
            self._playwright = None
