"""多账号管理 + 扫码登录会话。

- AccountWorker:每账号独立 Playwright 持久化 profile + 评论页 + 并行轮询
- AccountManager:管理 workers + 登录会话 + 自动评论热更新 + 状态上报
- 登录态检测:未登录跳登录页;LoginSession 负责扫码 + 字段抓取(见 login_capture.py)
"""
import os
import json
import time
import random
import hashlib
import asyncio
from datetime import datetime
import logging
logger = logging.getLogger("sphgj")
from playwright.async_api import async_playwright
from .api_client import WxApiClient
from .comment_fetcher import CommentFetcher
from .auto_reply import AutoReply
from .auto_delete import AutoDelete
from .live_fetcher import LiveFetcher, judge_live, _load_spec_cache
from .wx_http import build_cookie_header
from . import live_probe_http
from .auto_comment import AutoCommenter
from .login_capture import LoginSession, clear_login_state
from .browser import launch_stealth, close_context_safely, reap_profile_orphans
from .selectors import COMMENT_URL, POST_CREATE_URL
from .memtrim import MemoryTrimmer, trim_now


def auth_fingerprint(account):
    """登录态指纹 —— 只取 cookie 的 name=value 串(与 wx_http 取 cookie 的口径一致)。

    rev54: 用来判断「上次判离线之后,登录态有没有变过」。变了 = 用户重新扫码了,
    离线记录自动作废;没变 = 别重启一次又开一遍浏览器白验。
    空串表示本地压根没有可用 cookie(从未登录 / storage_state 被清空)。
    """
    try:
        h = build_cookie_header((account or {}).get("storage_state")) or ""
    except Exception:
        h = ""
    return hashlib.md5(h.encode("utf-8")).hexdigest() if h else ""


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
        # ===== rev54: 账号在网状态机 =====
        # unknown: 还没定论(刚建 worker);ok: 确定可用;offline: 登录态已失效。
        # 为什么要它:掉线账号原来的行为是「每轮白试一次 HTTP(300333)-> 回退浏览器
        # -> 开 Chromium 硬验 -> 未登录跳过」,下一轮再来一遍。用户 2026-10-28 要求:
        # 掉线判定要准确,定了就【不再发任何包】,直到扫码登录成功才回到轮询。
        self.auth_state = "unknown"
        self.auth_reason = ""
        self.auth_code = None
        self.auth_ts = 0.0
        self._online_event = asyncio.Event()
        self._online_event.set()          # 默认放行;判离线时 clear
        self.on_auth_change = None        # async (account_id, state, reason) -> None
        # 最近一次 auth_data 硬验的结论:"ok"/"no"/None(没验过/验不出)。判离线只认 "no"
        self._verify_result = None
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
        # rev16 F2: 判定信号丢失后原地重试 poll 的计数(信号恢复即清零)
        self._live_poll_retry = 0
        # ======================== rev47: 统一直播探测器 ========================
        # 原实现三个入口各管一段:_fetch_loop 里的 _cycle_live_check(跟 600s 抓取周期,
        # 夜间 ×3 = 30 分钟)、_live_probe_loop(仅手动启动的 180s 窗口)、_live_loop(在播时)。
        # 后果:① 开播最坏要等 30 分钟才发现;② 同一账号 90 秒内被检测两次
        # (实测 4 号 01:47:12 与 01:48:58);③ 每次检测开一整个直播大屏 SPA,141 秒才收敛。
        # rev47 起只留一个探测器:轻量探测(一次 HTTP)为主,周期性才落完整检测。
        self._detect_task = None
        self._probe_wake = asyncio.Event()
        self._probe_boost_until = 0.0   # 手动启动后的高频探测窗口(原 _probe_window 语义)
        self._last_probe_ts = 0.0       # 最近一次探测时间(探测器与抓取周期共用节流)
        self._last_probe = {}           # 最近一次探测结果(诊断用)
        self._probe_since_full = 0      # 距上次"完整检测"的轻量探测次数
        # 探测互斥:三个触发源(常驻探测器/抓取周期/手动窗口)必须先抢这把锁,
        # 锁内再确认一次 live_fetcher,避免并发探测(一个持 context 探测、另一个
        # 在同一 context 上新开 live_page,而后者不经过 _browser_lock)。
        self._probe_lock = asyncio.Lock()
        # rev48: 纯 HTTP 探测的"暂停尝试"截止时间。cookie 没了 / sessionid 过期时,
        # 3 分钟内不会自己恢复,没必要每轮都白等一次 8 秒超时后才回落浏览器路径。
        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""        # 最近一次 HTTP 探测不可用的原因(no_spec/no_cookie/expired…)

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
        # 归属视频号 getter:每次现取(登录/重登后 _log_finder_id 会变),
        # 评论、自动回复、自动删除、自动评论统一按视频号归属,不再按登录卡片
        _fid = (lambda: (self.account.get("_log_finder_id")
                         or self.account.get("locked_finder_id") or ""))
        self.auto_reply = AutoReply(None, self.storage, self.account["id"],
                                    self.config.get("auto_reply") or {}, finder_getter=_fid)
        self.auto_commenter = AutoCommenter(None, self.storage, self.account)
        self.auto_delete = AutoDelete(None, self.storage, self.account["id"],
                                      self.config.get("auto_delete") or {}, finder_getter=_fid)
        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete,
                                      finder_getter=_fid, config=self.config)
        # rev52: 先建一个「无浏览器」的 API 客户端。rev50 之后读接口全程走进程内 HTTP,
        # 浏览器只在 HTTP 走不通时才需要 —— 但原来这条路径必须先 ensure_browser()
        # 才能拿到 api/fetcher,等于每轮强制 launch Chromium(8~15s)+ goto + auth 硬验,
        # 8 个账号每天 120 轮 = 960 次启停。这里预建一个 page=None 的客户端,
        # _open_context() 之后仍会重建带 page 的版本覆盖(浏览器 = HTTP 的回退路径)。
        self.api = WxApiClient(None, self.account, self.config, self.storage)
        self.auto_reply.api = self.api
        self.auto_commenter.api = self.api
        self.auto_delete.api = self.api
        self.fetcher.api = self.api
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
        # rev47: 常驻探测器(统一节拍,取代原先"跟抓取周期 + 手动窗口"两套)
        self.start_live_detect()

    # ======================== 按需浏览器生命周期(省内存) ========================

    # ======================== rev54: 账号在网状态机 ========================

    @property
    def is_offline(self):
        return self.auth_state == "offline"

    async def mark_offline(self, reason, code=None):
        """判定登录态已失效:停轮询、停探测、关浏览器,不再向服务端发任何请求。

        证据必须是【确定性】的 —— HTTP 直连报 300333/300330,并且回退到真浏览器后
        auth_data 硬验仍给不出登录身份。其它错误(-1 / 超时 / 300342 会话被顶替)
        一律不判离线:那些要么会自己恢复,要么是重登窗口期的正常现象,误判会把
        好账号踢出轮询(300342 就是这么被误解成"限流"过的)。
        """
        if self.auth_state == "offline":
            if reason and reason != self.auth_reason:
                self.auth_reason = reason
            return
        self.auth_state = "offline"
        self.auth_reason = reason or ""
        self.auth_code = code
        self.auth_ts = time.time()
        self.logged_in = False
        self._online_event.clear()
        # 引用计数清零:掉线后不再有任何合法持有者。不清会让下次 ensure_browser 漏减,
        # context 永远关不掉(原来未登录分支每轮泄漏 +1 就是这么来的)。
        self._browser_refs = 0
        logger.warning(f"[{self.account['id']}] 判定登录态失效({self.auth_reason}"
                       + (f", errCode={code}" if code else "") +
                       f"),已退出轮询:不再发送任何请求,扫码登录成功后自动恢复")
        try:
            await self._close_context()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 掉线后关浏览器异常(忽略): {e}")
        await self._emit("account_offline", {"account_id": self.account["id"],
                                            "reason": self.auth_reason,
                                            "code": code, "ts": self.auth_ts})
        await self._notify_auth_change()

    async def mark_online(self, why=""):
        """登录态恢复可用:清失效标记,重新加入轮询(评论抓取 + 直播探测)。"""
        was = self.auth_state
        self.auth_state = "ok"
        self.auth_reason = ""
        self.auth_code = None
        self.auth_ts = time.time()
        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""
        try:
            if self.api is not None:
                self.api.clear_auth_fail()
        except Exception:
            pass
        self._online_event.set()
        if was != "ok":
            logger.info(f"[{self.account['id']}] 登录态可用"
                        + (f"({why})" if why else "") + ",已加入轮询")
            if self._running:
                self.start_loop()          # 内部幂等:已在跑不会重复起
                self.start_live_detect()
            await self._notify_auth_change()

    async def _notify_auth_change(self):
        cb = self.on_auth_change
        if cb is None:
            return
        try:
            await cb(self.account["id"], self.auth_state, self.auth_reason)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 在网状态回调异常(忽略): {e}")

    async def _wait_online(self, timeout=600.0):
        """离线等待:不抢抓取锁、不发包、不开浏览器。超时返回后由调用方重查状态。"""
        if self.auth_state != "offline":
            return
        try:
            await asyncio.wait_for(self._online_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _refresh_storage_state(self):
        """把浏览器现取的登录态回填 config.storage_state(rev54)。

        为什么必须做:cookie 的权威副本在浏览器里,config 里的 storage_state 只是
        扫码那一刻的快照,会逐渐过期。过期之后 HTTP 直连每次 300333 -> 每轮都得开
        一次 Chromium 才抓得动,白烧一次启停。这里在【硬验登录有效之后】顺手回填,
        指纹一变下一轮 HTTP 就恢复直连。

        只在 cookie 串真的变了才写盘,避免每轮重写 config.json。
        """
        if self.context is None:
            return False
        try:
            st = await self.context.storage_state()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 导出登录态失败: {e}")
            return False
        if not (st or {}).get("cookies"):
            return False
        old = auth_fingerprint(self.account)
        self.account["storage_state"] = st
        if auth_fingerprint(self.account) == old:
            return False
        cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")
        try:
            tmp = cfg + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cfg)
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 回填登录态写盘失败: {e}")
            return False
        logger.info(f"[{self.account['id']}] 已用浏览器现取会话刷新登录态"
                    f"(cookies={len(st.get('cookies') or [])} 条),HTTP 直连可恢复")
        return True

    # ============== 登录态真伪校验(rev37) ==============

    async def _probe_auth(self):
        """在页面内调 auth_data 硬验登录态,返回 (state, finder_id)。

        state: "ok" 真登录 / "no" 明确未登录 / "err" 拿不到结论(接口异常)。
        为什么必须调接口:视频号是异步鉴权,sessionid 服务端失效后 SPA 会先把
        /platform/interaction/comment 这个 URL 留住(先渲染壳,再请求鉴权),
        十几秒后才 location 跳 login.html。只看 URL 会把空壳判成已登录 ——
        账号卡片一直亮、点抓取全失败,用户根本看不出问题在哪(2026-10-21
        8 号实证:日志说已登录,18 秒后远程窗口打开是 login.html)。
        """
        try:
            aid = await self.page.evaluate(
                "() => localStorage.getItem('__ml::aid')"
                " || localStorage.getItem('__rx::aid') || ''")
        except Exception:
            aid = ""
        aid = (aid or "").strip().strip('"')
        try:
            txt = await self.page.evaluate("""async (aid) => {
                const u = `https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data?_aid=${aid}&_pageUrl=${encodeURIComponent('https://channels.weixin.qq.com/micro/interaction/comment')}`;
                const r = await fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}', credentials:'include'});
                return await r.text();
            }""", aid)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] auth_data 探活异常: {e}")
            return ("err", "")
        try:
            data = json.loads(txt or "{}")
        except Exception:
            return ("err", "")
        d = data.get("data")
        if not isinstance(d, dict):
            return ("err", "")
        fid = ((d.get("finderUser") or {}).get("finderUsername") or "").strip()
        if fid:
            return ("ok", fid)
        return ("no", "")

    async def _verify_login(self, wait_sec=10.0):
        """判定当前是否真的登录(rev37)。先硬验接口,拿不到结论再等 URL 跳转。"""
        deadline = time.time() + wait_sec
        while True:
            state, fid = await self._probe_auth()
            if state == "ok":
                # auth_data 给的 finder_id 比 localStorage 残留更权威,先记下
                if fid:
                    self.account["_log_finder_id"] = fid
                return True
            if state == "no":
                return False
            # 注意:只把「真在 login 页」当未登录。不能要求 URL 里必须有
            # interaction/comment —— 实测有效登录时深链也会被前端弹回
            # /platform(2 秒在 comment、4 秒回首页),那种判法会把好账号全判成
            # 未登录,进而触发无谓的重登。
            url = self.page.url or ""
            if "login" in url:
                return False
            if time.time() >= deadline:
                # 接口始终给不出结论 -> 回退 URL 判据(只看有没有被弹到登录页)
                logger.warning(f"[{self.account['id']}] auth_data 无有效响应,"
                               f"回退按 URL 判定登录态")
                return "login" not in url
            await self.page.wait_for_timeout(2000)

    async def _open_context(self):
        """懒加载开 Chromium:建 context+评论页,登录校验,建/重绑 API 客户端。幂等。"""
        if self.context is not None:
            return
        self._verify_result = None
        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        # rev29: 注入扫码时导出的登录态(双保险)。profile 磁盘 cookie 有时写入不成功
        # (Chromium 延迟写盘 + 进程被中途关闭),只靠磁盘会反复「扫码成功但登录不上」。
        try:
            _ss = (self.account.get("storage_state") or {})
            _ck = _ss.get("cookies") or []
            if _ck:
                await self.context.add_cookies(_ck)
                logger.info(f"[{self.account['id']}] 已注入登录态 cookies={len(_ck)} 条")
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 注入登录态失败(回退用 profile 磁盘): {e}")
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        # rev37: 不再只看 URL —— sessionid 失效时 SPA 会先把 comment 这个 URL
        # 留住十几秒才跳 login.html,固定 3 秒查 URL 会把空壳判成已登录。
        self.logged_in = await self._verify_login()
        # rev54: 记下硬验结论。判离线只认 "no" —— launch/goto 抛异常时这里是 None,
        # 那种情况只能算"没验成",不能当成"未登录"(否则网络抖动就把账号踢出轮询)
        self._verify_result = "ok" if self.logged_in else "no"
        if self.logged_in:
            logger.info(f"[{self.account['id']}] 已登录")
        else:
            logger.warning(f"[{self.account['id']}] 未登录(当前:{self.page.url}),请重新扫码")
        # _aid 每次登录会变,实时从 profile localStorage 读当前会话 _aid/_log_finder_id
        ls = {}
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
        # 锁定校验:profile 里现存的登录身份若与账号锁定的微信不符(历史脏登录态,比如
        # 之前用错微信扫过),直接按未登录处理 —— 引擎会走自动重登,用户用锁定微信重扫。
        # 否则会拿错的登录态抓数据,把别的账号的数据记到本账号名下。
        locked_fid = (self.account.get("locked_finder_id") or "").strip()
        cur_fid = (ls.get("finder_id") or "").strip()
        if self.logged_in and locked_fid and cur_fid and cur_fid != locked_fid:
            logger.warning(f"[{self.account['id']}] 当前登录身份与锁定微信不一致"
                           f"(登录={cur_fid[:12]}… 应={locked_fid[:12]}…),按未登录处理,需重新扫码")
            self.logged_in = False
            self._verify_result = "no"
        # rev38: 登录态真实可用时补齐 login_at、清掉 logout_at。
        # 必须放在锁定校验【之后】—— 上面身份不一致时会把 logged_in 置 False,
        # 那种情况不能记登录时间。
        if self.logged_in:
            await self._sync_login_time()
            # rev54: 浏览器里的 cookie 才是权威副本 —— 顺手回填 config.storage_state,
            # 让 HTTP 直连的登录态跟着一起刷新,不用每轮都开浏览器。
            await self._refresh_storage_state()
            await self.mark_online("浏览器 auth_data 硬验通过")
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

    async def _sync_login_time(self):
        """登录态确实可用时,把 login_at 补齐、logout_at 清掉(rev38)。

        为什么需要:login_at 原先只在扫码 finalize(login_capture.py)里写。而账号
        自动恢复走的是「注入 storage_state」和「profile 磁盘 cookie」两条路,都不写
        login_at;logout_at 一旦写过还会永久留在 config 里。于是出现
        login_at=None + logout_at=旧值、但 logged_in=true 的组合。

        后果有三个:
          1. 前端 LoginCountdown 只认 login_at,`!loginAt && logoutAt` 直接打
             「未登录」—— 用户看到的状态和后端完全相反
          2. publish_queue._login_remain_sec() 靠 login_at 算 24h TTL,拿不到就
             返回 0,于是 min_login_remain_sec 这道门槛被静默跳过
          3. config 里 logout_at 永远留着,再也没人清
        """
        acc = self.account
        changed = False
        if not (acc.get("login_at") or "").strip():
            acc["login_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
        if acc.get("logout_at"):
            acc.pop("logout_at", None)
            changed = True
        if not changed:
            return
        # 先写临时文件再 replace:config.json 是这个程序唯一的配置文件,
        # 直接覆盖写在写一半掉电时会整份坏掉。
        try:
            tmp = "config.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, "config.json")
            logger.info(f"[{acc.get('id')}] 已补齐登录时间 login_at={acc['login_at']}"
                        f"(自动恢复的登录没走过扫码流程)")
        except Exception as e:
            logger.warning(f"[{acc.get('id')}] 补齐 login_at 写盘失败: {e}")
            return
        await self._emit("accounts_updated",
                         {"reason": "login_time_sync", "account_id": acc.get("id")})

    async def _close_context(self):
        """关 context 收回 Chromium 进程(兜底 kill 残留 chrome)。清所有页面引用。"""
        ctx = self.context
        lf = self.live_fetcher
        self.context = None
        self.page = None
        self.live_page = None
        self.live_fetcher = None
        # rev54c: page 已经死了,但 self.api 还握着它 —— 后面任何「这次 HTTP 走不通、
        # 回退页面 fetch」的请求都会拿着死 page 去 evaluate,报 Target closed,
        # 还会被记成一次失败触发 30s 退避。这里换回无浏览器客户端,等 _open_context 重建。
        # auth_fail / _http_block_until 必须继承:否则「cookie 已死,先别试 HTTP」的
        # 退避状态会被重建抹掉,下一轮又拿着死 cookie 去撞 300333。
        if self.api is not None:
            old_api = self.api
            self.api = WxApiClient(None, self.account, self.config, self.storage)
            self.api.auth_fail = getattr(old_api, "auth_fail", None)
            self.api._http_block_until = getattr(old_api, "_http_block_until", 0.0)
            self.api._http_logged = getattr(old_api, "_http_logged", False)
            for comp in (self.auto_reply, self.auto_commenter, self.auto_delete, self.fetcher):
                if comp is not None:
                    comp.api = self.api
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

    # ======================== rev47: 轻量直播探测(不开大屏页) ========================

    async def _open_context_light(self):
        """只建 context(注入登录态),不导航评论页 —— 专供轻量直播探测。

        与 _open_context 的区别:不 goto COMMENT_URL、不 wait 2.5s、不建 WxApiClient、
        不做登录态硬验(那是评论抓取要用的)。探测只需 cookie 在场,一次
        check_live_status 就够,省掉 3~5 秒页面加载和一个 renderer。
        调用方必须持有 _browser_lock,用完自己 close(绝不写回 self.context)。
        """
        ctx = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        try:
            _ck = ((self.account.get("storage_state") or {}).get("cookies") or [])
            if _ck:
                await ctx.add_cookies(_ck)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测注入登录态失败: {e}")
        return ctx

    async def _probe_via_context(self, ctx):
        """用一次 check_live_status 请求探测直播状态。返回判据 dict 或 None。

        走 APIRequestContext(ctx.request)而不是 page.evaluate:它共享同一个 cookie jar,
        但完全不经过 renderer —— 没有页面加载、没有 SPA 水合、没有网络事件进 Python。
        这是"把探测频率提到分钟级"的前提。

        解析仍复用 LiveFetcher._parse_live_status(构造一个 page=None 的临时实例),
        保证"轻量探测"和"大屏页探测"对同一份响应得出同样的结论 —— 不要再写第二份解析。
        """
        spec = (_load_spec_cache(self.account["id"]) or {}).get("live_status")
        if not spec or not spec.get("url"):
            return None
        headers = {k: v for k, v in (spec.get("headers") or {}).items()
                   if k.lower() not in ("cookie", "content-length", "host")}
        body = spec.get("postData")
        url = spec["url"]
        try:
            if (spec.get("method") or "POST").upper() == "GET":
                r = await ctx.request.get(url, headers=headers, timeout=15000)
            else:
                r = await ctx.request.post(url, headers=headers, data=body or None, timeout=15000)
            code = r.status
            txt = await r.text()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测请求失败: {e}")
            return None
        if not txt or code >= 400:
            logger.debug(f"[{self.account['id']}] 轻量探测响应异常(status={code}, len={len(txt or '')})")
            return None
        lf = None
        try:
            lf = LiveFetcher(None, self.account["id"], account=self.account)
            # 抑制"拿到 liveObjectId, dashboard 抓取启用"这类只对常驻实例有意义的日志
            # 与首次落盘(轻量探测每几分钟一次,不该重复写文件)
            lf.live_object_id = "-"
            lf._status_body_dumped = True
            await lf._parse_live_status(txt)
            live, reason = judge_live(
                live_stats=lf.live_stats, stats_fresh=True,
                stream_url=lf.stream_url, allow_stream_url=True,
                status=lf.status_num)
            return {"ts": time.time(), "live": live, "reason": reason,
                    "status": lf.status_num, "has_stream": bool(lf.stream_url),
                    "has_stats": bool(lf.live_stats), "via": "light"}
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测解析失败: {e}")
            return None
        finally:
            lf = None
            txt = None

    async def _probe_via_http(self):
        """rev48: L0 纯 HTTP 探测 —— 不碰浏览器、不拿 _browser_lock、不建 context。

        为什么这条能成立见 `backend/live_probe_http.py` 的文件头(2026-10-28 实测:
        HTTP 201 / errCode=0 / 1.0s / wxNickname 与账号对得上)。一句话:
        接口规格在 data/live_api_spec.json,登录态在 storage_state.cookies,两样凑齐
        就能直发 check_live_status,不需要浏览器。

        cookie 用 config 里的**最新**账号对象取(重新扫码后 storage_state 会被换掉),
        拿不到才回退 self.account —— 否则扫码前的旧 worker 会一直以为没 cookie。
        """
        if time.time() < self._http_block_until:
            return None
        acc = self.account
        try:
            for a in (self.config.get("accounts") or []):
                if a.get("id") == self.account.get("id"):
                    acc = a
                    break
        except Exception:
            pass
        diag = {}
        try:
            res = await live_probe_http.probe_http(acc, diag)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] HTTP 探测异常: {e}")
            return None
        if res is not None:
            self._http_block_until = 0.0
            self._http_block_logged = ""
            return res
        why = diag.get("why") or ""
        self._http_last_why = why
        if why in ("no_cookie", "badauth", "expired"):
            # 这两类 3 分钟内不会自己恢复:与其每轮白等一次超时,不如先暂停 HTTP,
            # 直接走浏览器路径(那边有真会话,能判)。cookie 一被重新扫码写入,
            # storage_state 变了,下一轮 build_cookie_header 就有值了 -> 自动恢复。
            self._http_block_until = time.time() + 180
            if self._http_block_logged != why:
                self._http_block_logged = why
                if why == "no_cookie":
                    logger.warning(f"[{self.account['id']}] HTTP 探测不可用:该账号没有已保存的"
                                   f"登录态(storage_state 无 cookie),改用浏览器路径探测"
                                   f"(重新扫码后会自动恢复纯 HTTP 探测)")
                elif why == "expired":
                    logger.warning(f"[{self.account['id']}] HTTP 探测被拒 errCode=300333:"
                                   f"登录态已失效(sessionid 过期),该账号需要重新扫码;"
                                   f"本次先按未登录处理,不做浏览器探测")
                else:
                    logger.warning(f"[{self.account['id']}] HTTP 探测被拒(errCode={diag.get('code')}),"
                                   f"改用浏览器路径探测")
        elif why == "request_error":
            logger.debug(f"[{self.account['id']}] HTTP 探测请求失败: {diag.get('err')}")
        return None

    async def _probe_light(self):
        """轻量探测。返回判据 dict 或 None(不可用)。

        L0 纯 HTTP(rev48):用 storage_state 的 cookie + 已学到的接口规格直发
           check_live_status,实测 ~1s,**不启任何浏览器进程**。这是常态路径。
        L1 已有 context(正在抓评论/手动操作)-> ctx.request 重放,约 0.3s,零新增开销;
        L2 没有 context -> 只建 context 探完即关,约 3~6s(远小于开大屏页的 30~140s)。
        L0~L2 全失败 -> 返回 None,调用方退回完整检测(开直播大屏页)。

        L1/L2 不是冗余:L0 要求 storage_state 里有 cookie,没扫过码的账号没有;
        而且 cookie 真失效时,只有真浏览器的现取会话还可能判得出来。
        """
        # rev54: 掉线账号不探测 —— 连 L1/L2 都不该走,那两个会开浏览器
        if self.auth_state == "offline":
            return None
        hit = await self._probe_via_http()
        if hit is not None:
            return hit
        # 借不到任何规格、手上又没有现成 context:L1/L2 也发不出请求(重放同样要规格),
        # 起一个 Chromium 纯属浪费 —— 直接交给完整检测去学规格(学完落盘,下轮 HTTP 就能用)
        if self._http_last_why == "no_spec" and self.context is None:
            return None
        try:
            async with self._browser_lock:
                if self.context is not None:
                    return await self._probe_via_context(self.context)
                try:
                    ctx = await self._open_context_light()
                except Exception as e:
                    logger.debug(f"[{self.account['id']}] 轻量探测开浏览器失败: {e}")
                    return None
                try:
                    res = await self._probe_via_context(ctx)
                    if res is not None:
                        # 标记"这一轮真的拉起了 Chromium" —— 节拍器据此降频,
                        # 别让 cookie 失效的账号被高频探测反复开浏览器(母本 OOM 过)
                        res["started_browser"] = True
                    return res
                finally:
                    try:
                        await close_context_safely(ctx, self.account.get("profile_dir"),
                                                   f"[{self.account['id']}] 轻量探测收回")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测异常: {e}")
            return None

    async def _probe_and_maybe_open(self):
        """rev47: 唯一探测入口 —— 轻量判定,命中或周期性才开直播大屏页。返回 is_live。

        探测器循环 / 评论抓取周期 / 手动启动窗口都走这里,共用 _last_probe_ts 节流。
        """
        if self.live_fetcher is not None:
            return True
        if not self.logged_in:
            return False
        async with self._probe_lock:
            # 锁内二次确认:等锁期间可能已经有人探测完并把 live_page 开起来了
            if self.live_fetcher is not None:
                return True
            self._last_probe_ts = time.time()
            probe = await self._probe_light()
            return await self._after_probe(probe)

    async def _open_full(self, fast=False):
        """完整检测(开直播大屏页)。探测器可能在没有浏览器时调用,这里负责补引用。"""
        if self.context is not None:
            return await self._open_and_detect_live(fast=fast)
        if not await self.ensure_browser():
            logger.debug(f"[{self.account['id']}] 完整检测跳过:开浏览器失败")
            return False
        try:
            return await self._open_and_detect_live(fast=fast)
        finally:
            await self.release_idle_browser()

    async def _after_probe(self, probe):
        """轻量探测结果 -> 是否开直播大屏页。probe=None 表示轻量不可用,退回完整检测。"""
        if probe is not None:
            self._last_probe = probe
            self._probe_since_full += 1
            if probe["live"]:
                logger.info(f"[{self.account['id']}] 轻量探测:在播(依据={probe['reason']}, "
                            f"via={probe.get('via')}),打开直播大屏页取流")
                return await self._open_full()
            # 未播:轻量探测不开大屏页,直接收工。
            # rev49(2026-10-28 用户决策 live_probe_full_every 默认 0):**永不周期性落完整检测**。
            # 原默认 5 + interval 120s = 每 10 分钟给每个账号开一次 Chromium + goto 大屏页 +
            # 等 4 秒;8 个账号每小时 48 次浏览器启停,而这些账号全都不开播 —— 纯浪费(用户原话
            # "收益是零")。关掉后未播时只有 1.1 秒的纯 HTTP 请求,浏览器一次都不开;
            # 真开播时 probe["live"] 走上面的 _open_full(),大屏页照样打开,功能不减。
            # 需要恢复 DOM 文本兜底判据时把它设成 >0(每 N 次未播落一次完整检测)。
            full_every = int(self.config.get("live_probe_full_every", 0) or 0)
            if full_every > 0 and self._probe_since_full >= full_every:
                self._probe_since_full = 0
                logger.info(f"[{self.account['id']}] 轻量探测连续 {full_every} 次未播"
                            f"(status={probe.get('status')}),做一次快速完整检测(含页面文本)")
                return await self._open_full(fast=True)
            # INFO 而非 DEBUG:轻量探测是 rev47 的核心链路,必须在 run.log 直接看到它的节拍,
            # 确认它在跑而不是在空转。rev49: full_every=0 时不再显示无意义的 N/0 计数。
            note = (f"第 {self._probe_since_full}/{full_every} 次"
                    if full_every > 0 else "不做完整检测")
            logger.info(f"[{self.account['id']}] 轻量探测:未播(依据={probe['reason']},"
                        f"status={probe.get('status')}, via={probe.get('via')}, {note})")
            return False
        return await self._open_full()

    async def _open_and_detect_live(self, fast=False):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。

        fast=True:轻量探测已明确"没信号"时的快速确认 —— 等 4 秒即可,不做 reload 重试。
        依据:未开播时直播控制台会被 SPA 弹回 /platform/,页面不会请求 get_live_info,
        再怎么等也等不到 liveStats(实测 02:03~02:05)。真在播时轻量探测会先命中,
        根本走不到这里,所以不会有假阴性。

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
            lf = LiveFetcher(self.live_page, self.account["id"], account=self.account)
            self.live_fetcher = lf
            await lf.goto_live()
            self._live_opened_ts = time.time()
            self._live_nosignal_reloads = 0
            # rev47: 接口层没有"在播"布尔位(未开播时只有 status=0),所以页面文本
            # 是最廉价、最不依赖接口字段的兜底判据。但这里刚 goto 完(domcontentloaded),
            # SPA 还没渲染,读到的多半是空 —— 只作预热,真正的采样在判定前做。
            try:
                await lf.read_dom_state(dump=False)
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 预热读直播页文本失败: {e}")
            info = await self._wait_for_live_detection(
                lf, timeout=4.0 if fast else 20.0, allow_relearn=not fast)
            # rev47: 判定前再采一次页面文本 —— 这时已经等过 20~32 秒,直播大屏页渲染完成,
            # 这次的结果才进判据;诊断 dump 也只在这里做(每轮检测一份)。
            try:
                await lf.read_dom_state(dump=True)
                info = await lf.snapshot()
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 判定前读直播页文本失败: {e}")
            # 判定:有 live_stats(近期)即直播;stream_url(.flv 流)也视为直播(视频流必出)。
            # 【rev13 修复 flapping】live_object_id 单独【不再】作为直播信号:
            # 它可能来自上一场直播残留的 check_live_status 响应(账号已下播但接口仍返回旧
            # liveObjectId),若据此判直播会反复"开播→下播"开关浏览器(见某账号抖动案例)。
            # live_object_id 仅用于 dashboard 抓取,不参与 is_live 判定。
            now_ts = time.time()
            last_ts = lf.updated_at_ts or 0
            # rev47: 统一判据(见 live_fetcher.judge_live)。检测窗口只有 30 秒,
            # 不存在"上一场残留的 sticky 流地址",所以这里允许用 stream_url。
            is_live, why = judge_live(
                live_stats=info.get("live_stats"), stats_fresh=(now_ts - last_ts) < 30,
                stream_url=lf.stream_url, allow_stream_url=True,
                dom_state=info.get("dom_state"),
                dom_fresh=(now_ts - (info.get("dom_ts") or 0)) < 120,
                status=info.get("status_num"))
            if is_live:
                logger.info(f"[{self.account['id']}] 检测到直播信号(依据={why}),开始流式推送")
                self.start_live_loop()
                logger.info(f"[{self.account['id']}] 检测到直播,开始流式推送"
                            f"(live_object_id={lf.live_object_id}, "
                            f"stream_url={'有' if lf.stream_url else '待取'})")
                return True
            # 非直播:关页回收(检测窗口内未拿到任何直播信号)
            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(依据={why}, live_stats={info.get('live_stats') is not None}, "
                        f"status={info.get('status_num')}, dom={info.get('dom_state')}, "
                        f"live_object_id={lf.live_object_id}, "
                        f"learned={sorted(getattr(lf, '_learned', {}) or {})}, "
                        f"cdp_fallback={getattr(lf, '_cdp_fallback', None)}, "
                        f"url={(info.get('page_url') or '')[-40:]})")
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

    async def _wait_for_live_detection(self, lf, timeout=20.0, allow_relearn=True):
        """goto 之后被动等待 check_live_status/get_live_info 响应到达(最多 timeout 秒)。

        用 snapshot()(不告警)轮询,避免检测阶段误触发"stream_url 为空"告警;
        一旦取到 live_stats / live_object_id / stream_url 任意一项即认为响应已到,立即返回。
        """
        # rev44: 提前返回条件必须与 _open_and_detect_live 的判定口径一致 ——
        # 判定只认 live_stats / stream_url,live_object_id 不参与(可能是上一场残留)。
        # 原实现拿 live_object_id 就提前 return,于是等不到 get_live_info 的 liveStats,
        # 明明在播也被判"未在直播"。
        def _hit(i):
            # rev47: 与 _open_and_detect_live 共用同一个判据函数。
            # rev44 那次事故就是这里与判定各写一套(拿 live_object_id 就提前返回),
            # 以后不许再就地写表达式。
            ok, _ = judge_live(
                live_stats=i.get("live_stats"), stats_fresh=True,
                stream_url=lf.stream_url, allow_stream_url=True,
                dom_state=i.get("dom_state"),
                dom_fresh=(time.time() - (i.get("dom_ts") or 0)) < 120,
                status=i.get("status_num"))
            return ok

        async def _round(secs):
            deadline = time.time() + secs
            info = {}
            while time.time() < deadline:
                # rev13: 主动轮询(替代被动等响应)——页面加载后由 fetcher 主动拉取直播数据
                await lf.poll()
                info = await lf.snapshot()
                if _hit(info):
                    return info
                await asyncio.sleep(2)
            return info or {}

        info = await _round(timeout)
        if _hit(info):
            return info
        # rev43: 首轮没信号【不再直接判下播】。学习超时点在第一轮尾声,剩下的几秒只够
        # CDP 被动监听空等(页面早已把 check_live_status 发完)-> 必然误判。重载一次直播页
        # 让请求重新发出,再给一轮时间。
        if not allow_relearn:
            return info or {}
        try:
            if await lf.relearn():
                logger.info(f"[{self.account['id']}] 首轮未取到直播信号,重载直播页重试一轮")
                info = await _round(12.0)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 重载重试失败: {e}")
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
        # rev47: 抓取结束时 context 还开着(L0),这次探测几乎零成本 —— 只是多一个
        # check_live_status 请求,不再为此另开一个直播大屏页。
        await self._probe_and_maybe_open()

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
            # rev16 F3: 直播期间评论抓取降频。
            # 直播中评论主要由直播间产生(live_page 侧流式处理),而 fetch_all() 是遍历该账号
            # 【全部视频】逐个分页拉评论 —— 开销大(ensure_browser + 遍历视频 + 分页请求)
            # 但直播期间收益极低。账号在播时改用 live_fetch_interval_sec(默认 3600s),
            # 省约 83% 抓取开销;设为 0 则直播期间完全跳过(直播检测仍由 _live_loop 负责)。
            live_fetch_interval = int(self.config.get("live_fetch_interval_sec", 3600))
            if self.live_fetcher is not None and live_fetch_interval == 0:
                await asyncio.sleep(30)
                continue
            # rev54: 掉线账号【不进轮询】—— 不抢锁、不发包、不开浏览器,等 mark_online()
            # (扫码登录成功)唤醒。超时只是兜底重查一次状态,本身不产生任何请求。
            if self.auth_state == "offline":
                await self._wait_online(min(int(interval or 600), 600))
                continue
            # 串行抓取:全局队列锁,保证多账号不同时拉起浏览器(省内存 + 防风控关联)
            if self._fetch_lock is not None:
                await self._fetch_lock.acquire()
            used_browser = False
            try:
                res, used_browser = await self._run_fetch_round()
                if res is None:
                    await asyncio.sleep(min(interval, 60))
                    continue
                scanned, new_c, new_comments, deleted_ids, complete = res
                self.last_scan_at = datetime.now().isoformat()
                if new_c:
                    today = datetime.now().strftime("%Y-%m-%d")
                    if self._new_count_date != today:
                        self._new_count_date = today
                        self.new_count = 0
                    self.new_count += new_c
                if not complete:
                    # rev52: 原来这种情况静默中断 —— 打的是「扫描N视频 新增0评论」INFO,
                    # 看起来这轮成功了,其实后面的作品压根没检查。
                    logger.warning(f"[{self.account['id']}] 本轮抓取不完整:有请求连续失败,"
                                   f"部分作品未检查(未覆盖部分下一轮会重新扫)")
                if new_comments:
                    await self._emit("comments_update",
                                     {"account_id": self.account["id"], "comments": new_comments})
                for cid in deleted_ids:
                    await self._emit("comment_deleted", {"comment_id": cid})
                # 周期内顺带检测直播(非直播则收回进程)
                await self._cycle_live_check()
            finally:
                if self._fetch_lock is not None:
                    try: self._fetch_lock.release()
                    except Exception: pass
                # 无动作(非直播/无手动操作)则收回浏览器进程省内存。
                # rev52: 只有本轮真的开过浏览器才释放 —— 纯 HTTP 那一轮没有对应的
                # ensure_browser(+1),无条件 release 会吃掉手动操作/直播持有的引用计数。
                if used_browser:
                    await self.release_idle_browser()
            # 夜间降频(0~6 点间隔拉长)+ 抖动
            # rev16 F3: 账号在播时改用直播专用间隔(已足够长,不再叠加夜间倍率 ——
            # 夜间倍率是为风控降频设计,3600s 本身即很保守,叠加后单夜仅 0~1 次意义不大,
            # 且会让"直播结束前后的评论"延迟过久才被捕获)。
            if self.live_fetcher is not None and live_fetch_interval > 0:
                cur = live_fetch_interval
            else:
                cur = interval
                if _in_night_hours(night_hours):
                    cur = int(interval * night_mult)
            await asyncio.sleep(cur + random.uniform(0, min(cur, 30)))

    async def _run_fetch_round(self):
        """一轮评论抓取:HTTP 直连优先,只有走不通时才开浏览器。

        rev52: 原来每轮开头无条件 ensure_browser()。既然读接口(rev50)已经在进程内
        发 HTTP,浏览器就只在「HTTP 不可用」时才有价值 —— 典型是 6/9/10 号那种
        profile 磁盘上还登录着、但 config.storage_state 已过期(errCode=300333)的账号。

        返回 (5 元组, 是否用了浏览器);连登录态都拿不到时返回 (None, False)。

        rev54: 已判掉线(auth_state=offline)的账号一个包都不发,直接返回 ——
        这是用户 2026-10-28 定的规则:掉线状态准确就够了,不需要每轮去验证一遍。
        """
        if self.fetcher is None:
            return None, False
        if self.auth_state == "offline":
            logger.info(f"[{self.account['id']}] 登录已失效(待扫码),本轮不发任何请求")
            return None, False
        # 1) 纯 HTTP:不需要任何 Chromium 进程
        http_ok = False
        if self.api is not None:
            try:
                http_ok = bool(self.api.http_available())
            except Exception:
                http_ok = False
        if http_ok:
            try:
                res = await self.fetcher.fetch_all(on_batch=self._emit_comment_batch)
                if res[4]:
                    if self.context is None:
                        logger.debug(f"[{self.account['id']}] 本轮抓取全程 HTTP 直连(未开浏览器)")
                    return res, False
                logger.info(f"[{self.account['id']}] HTTP 直连本轮不完整,开浏览器重跑(补漏)")
            except Exception as e:
                logger.warning(f"[{self.account['id']}] HTTP 抓取异常,改用浏览器: {e}")
        # rev54: context 已经开着时 ensure_browser 不会重做 auth 硬验,拿的是上次的
        # logged_in。登录态半路失效的账号会「看起来还登录着」然后整轮失败,永远进不了
        # 离线判定。这里在 HTTP 明确报过 300333/300330 之后主动重验一次(证据链补全)。
        if self.context is not None and self.api is not None and getattr(self.api, "auth_fail", None):
            try:
                self.logged_in = await self._verify_login(wait_sec=5.0)
                self._verify_result = "ok" if self.logged_in else "no"
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 复用 context 补验登录态异常: {e}")
        # 2) 浏览器回退
        ok = await self.ensure_browser()
        if not ok:
            if self._verify_result == "no":
                # rev54: 这是「准确判定掉线」的完整证据链 —— HTTP 侧 cookie 不可用/
                # 被拒,回退到真浏览器之后 auth_data 硬验仍拿不到登录身份。
                code = None
                try:
                    code = (self.api.auth_fail or {}).get("code") if self.api else None
                except Exception:
                    code = None
                await self.mark_offline("未登录(HTTP 与浏览器硬验均无有效登录态)",
                                        code=code)
            else:
                # 开浏览器失败/硬验无结论 -> 只是本轮不成,不判离线(别把网络抖动当掉线)
                logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败或无法判定登录态,"
                               f"跳过本轮(不判离线)")
            # 注意:这里必须返回 True。ensure_browser 已经 +1,不 release 就会每轮
            # 漏一个引用,context 永远关不掉(原来掉线账号常驻 Chromium 的根因)。
            return None, True
        if not self.logged_in:
            return None, True
        try:
            return await self.fetcher.fetch_all(on_batch=self._emit_comment_batch), True
        except Exception as e:
            logger.error(f"[{self.account['id']}] 抓取异常: {e}")
            return None, True

    async def fetch_once(self):
        """手动「立即抓取一次」。同样先试 HTTP,失败再开浏览器。"""
        res, used_browser = await self._run_fetch_round()
        if used_browser:
            await self.release_idle_browser()
        return res if res is not None else (0, 0, [], [], False)

    async def _emit_comment_batch(self, comments, deleted_ids):
        """rev15 P2: 评论流式推送回调(由 CommentFetcher 在缓冲达阈值时调用)。

        与抓取结束后的最终推送走同一套事件(comments_update / comment_deleted),
        前端无需改动;分批推送让缓冲恒定在阈值规模,峰值内存从 O(总评论数) 降到 O(阈值)。
        """
        if comments:
            await self._emit("comments_update",
                             {"account_id": self.account["id"], "comments": comments})
        for cid in deleted_ids:
            await self._emit("comment_deleted", {"comment_id": cid})

    def start_live_loop(self):
        if self._live_loop_task is None or self._live_loop_task.done():
            self._live_loop_task = asyncio.create_task(self._live_loop())

    def start_live_probe(self):
        """手动启动后进入高频探测窗口,尽快捕获"启动后开播"。

        rev47: 不再单独起一个 _live_probe_loop(原来要自己借 _fetch_lock、每 24s
        重新开一次浏览器)。改为给统一探测器打一个 boost 标记并唤醒它 —— 窗口内
        间隔降到 live_probe_boost_interval_sec(默认 20s),窗口结束自动回落。
        """
        self._probe_boost_until = time.time() + self._probe_window
        try:
            self._probe_wake.set()
        except Exception:
            pass
        logger.info(f"[{self.account['id']}] 开播探测窗口 {int(self._probe_window)}s 内高频探测"
                    f"(间隔 {int(self.config.get('live_probe_boost_interval_sec', 20))}s)")

    def start_live_detect(self):
        """rev47: 启动统一探测器(每个账号一个,常驻但绝大多数时间在睡)。"""
        if self._detect_task is None or self._detect_task.done():
            self._detect_task = asyncio.create_task(self._live_detect_loop())

    async def _live_detect_loop(self):
        """rev47: 全账号唯一的直播探测节拍器。

        频率解耦:评论抓取仍是 fetch_interval_sec(600s,夜间 ×3),直播探测走独立的
        live_probe_interval_sec(默认 120s),【不再乘夜间倍率】—— 直播本来就集中在
        夜里,原来的 ×3 让开播最坏 30 分钟才发现。
        在播期间本循环只是睡(交给 _live_loop),不参与任何探测。

        rev48 快慢双档:纯 HTTP 探测约 1s、零浏览器,所以默认档位从 300s 提到 120s;
        但如果上一轮是靠【新建 Chromium】(L2)才判出来的(典型:账号没有 storage_state
        或 sessionid 过期),就退回 live_probe_slow_interval_sec(默认 300s),
        避免高频开浏览器 —— 母本 OOM 过一次,这个代价必须让频率来背。
        """
        # rev52: 未播间隔 120s -> 300s。轻量 HTTP 探测单次约 1.1s,8 个账号 120s 一轮
        # = 5760 次/天(占总请求 12%),而未播时探得再勤也只是"未播"。
        # 开播发现延迟由 180s 快速捕获窗口(start_live_probe 的 boost 30s)兜住,
        # 用户手动点检测也会立刻唤醒(Event),所以降频不影响"开播即被发现"。
        interval = max(30, int(self.config.get("live_probe_interval_sec", 300)))
        slow_interval = max(interval, int(self.config.get("live_probe_slow_interval_sec", 300)))
        boost_interval = max(5, int(self.config.get("live_probe_boost_interval_sec", 20)))
        gap = float(self.config.get("live_probe_min_gap_sec", 60))
        await asyncio.sleep(random.uniform(0, 20))   # 多账号错峰,避免同时拉起浏览器
        while self._running:
            wait = interval
            try:
                self._probe_wake.clear()
                if self.live_fetcher is None and self.logged_in and self.auth_state != "offline":
                    boost = time.time() < self._probe_boost_until
                    costly = bool((self._last_probe or {}).get("started_browser"))
                    wait = boost_interval if boost else (slow_interval if costly else interval)
                    # 节流:抓取周期可能刚顺带探过,别立刻再探一次
                    if time.time() - (self._last_probe_ts or 0) >= gap:
                        await self._probe_and_maybe_open()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[{self.account['id']}] 直播探测循环异常: {e}")
                wait = 60
            try:
                # 用 Event 等,start_live_probe / 手动检测可以立刻唤醒,不必等满 interval
                await asyncio.wait_for(self._probe_wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

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
                # 每 ~30 分钟 close+重建 live_page(替代 reload):reload 不退出 Chromium renderer,
                # 直播期间 FLV 流涨到的峰值内存不归还 OS;close page 触发 renderer 退出归还内存,
                # 新 page 起新 renderer 重新加载,长跑内存增长比 reload 彻底。
                # rev15 P4: 周期 48min -> 30min(config.live_page_rebuild_sec 可调)。
                # 依据:长时间直播(6h+)时 renderer 的 JS 堆/DOM 树仍缓慢增长,缩短周期可更频繁
                # 释放;重建有约 15s 数据空窗(转诊断用 pending 标记),30min 是空窗频率与内存增长的折中。
                self._last_live_reload = getattr(self, "_last_live_reload", 0.0)
                rebuild_sec = int(self.config.get("live_page_rebuild_sec", 1800))
                if self.live_page and now - self._last_live_reload >= rebuild_sec:
                    rebuilt = False
                    try:
                        # rev16 F4: 先取出旧实例已学的 API 规格,重建后继承(省去重学空窗)。
                        # 必须在 close() 前取值,close() 可能清空 _learned。
                        inherited_learned = dict(lf._learned) if (lf and getattr(lf, "_learned", None)) else None
                        if lf:
                            await lf.close()  # P0: 清回调+挂起 task
                        await self.live_page.close()         # 关 page 促 renderer 退出
                        self.live_page = await self.context.new_page()
                        lf = LiveFetcher(self.live_page, self.account["id"],
                                         learned=inherited_learned,
                                         account=self.account)
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
                    # rev16 F1: is_live 判定窗口 30s -> live_signal_lost_sec(默认 90s)。
                    # 原 30s 过紧:poll 是 page.evaluate(fetch) 重放,单轮偶发失败(页面卡顿、
                    # 网络抖动、JS 上下文繁忙)就会让 updated_at_ts 停滞,30s 一到即误判
                    # "信号丢失" -> 销毁重建 LiveFetcher(重学 API + reload 页面 + 数秒空窗),
                    # 代价远大于瞬断本身。放宽到 90s 可容忍连续多轮失败。
                    last_ts = lf.updated_at_ts or 0
                    signal_lost_sec = int(self.config.get("live_signal_lost_sec", 90))
                    # rev47: 没有 live_stats 时补一次页面文本采样(最多每 30s 一次),
                    # 这是"接口不认、页面认"的兜底判据 —— 长驻页面最容易遇到接口静默。
                    if not info.get("live_stats") and now - (getattr(lf, "dom_ts", 0) or 0) > 30:
                        try:
                            await lf.read_dom_state(dump=False)
                            info["dom_state"] = lf.dom_state
                            info["dom_ts"] = lf.dom_ts
                        except Exception:
                            pass
                    # rev47: 统一判据。长驻页面【禁止】用 stream_url 判活 —— 它是 sticky 的,
                    # 拿到一次就再也不清空,用它判活会永远判"在播"、永远不回收页面。
                    _sig, _why = judge_live(
                        live_stats=info.get("live_stats"),
                        stats_fresh=(now - last_ts) < signal_lost_sec,
                        allow_stream_url=False,
                        dom_state=info.get("dom_state"),
                        dom_fresh=(now - (info.get("dom_ts") or 0)) < 90,
                        status=info.get("status_num"))
                    info["is_live"] = _sig
                    info["live_reason"] = _why
                    if not info["is_live"]:
                        # rev16 F2: 判定信号丢失后,先原地快速重试 poll(不销毁重建)。
                        # 绝大多数瞬断在 1~2 轮重试内恢复;仅重试耗尽才走代价高昂的 reload/关页。
                        max_retry = int(self.config.get("live_poll_retry", 3))
                        retry_gap = int(self.config.get("live_poll_retry_gap_sec", 8))
                        if (self._live_poll_retry or 0) < max_retry:
                            self._live_poll_retry = (self._live_poll_retry or 0) + 1
                            _gap = "从未取到信号" if not last_ts else f"距今{int(now - last_ts)}s"
                            logger.info(f"[{self.account['id']}] 直播信号中断"
                                        f"({_gap}),原地重试 poll "
                                        f"(第{self._live_poll_retry}次/最多{max_retry}次)")
                            await asyncio.sleep(retry_gap)
                            continue
                        # 重试耗尽:确认真丢失,走原自愈/关页流程
                        # 自恢复:账号实际可能在播,但 check_live_status 捕获偶发瞬断(长驻页面常见)。
                        # 信号丢失 >120s 不直接关页,而是重载 live_page 重触发捕获(最多 3 次,间隔 60s),
                        # 成功拿到信号则继续流式;彻底失败(3 次仍无)才判定下播关页回收。
                        idle = (now - self._live_opened_ts) if last_ts == 0 else (now - last_ts)
                        page_alive = bool(self.live_page) and "liveBuild" in (self.live_page.url or "")
                        can_reload = page_alive and (self._live_nosignal_reloads or 0) < 3 \
                            and (now - (self._live_last_reload_attempt or 0)) > 60
                        if idle > 120 and can_reload:
                            self._live_nosignal_reloads = (self._live_nosignal_reloads or 0) + 1
                            self._live_last_reload_attempt = now
                            try:
                                # rev16 F4: 先取出旧实例已学的 API 规格,重建后继承,
                                # 避免重新走 requestWillBeSent 学习流程(省去数秒恢复空窗)。
                                # 注意必须先取值再 close(),close() 可能清空 _learned。
                                inherited_learned = dict(lf._learned) if (lf and getattr(lf, "_learned", None)) else None
                                if lf:
                                    await lf.close()
                                await self.live_page.reload(wait_until="domcontentloaded")
                                lf = LiveFetcher(self.live_page, self.account["id"],
                                                 learned=inherited_learned,
                                                 account=self.account)
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
                    # rev16 F2: 信号恢复即清零 poll 重试计数,下次真瞬断时重新走满重试额度
                    self._live_poll_retry = 0
                    # dashboardV4 指标,按 config.dashboard_interval_sec 抓(需 live_object_id,账号在直播)
                    if lf.live_object_id and now - (lf._dashboard_ts or 0) >= dash_interval:
                        try:
                            # rev21: 超时闸 —— dashboard 抓取内部已逐级加超时,这里再加一道
                            # 总闸。缺了它,任何一次挂起都会让整个 _live_loop 停摆(不再
                            # 推 live_screen_update),前端表现为"大屏彻底不动了"。
                            dd = await asyncio.wait_for(lf.fetch_dashboard_data(), 45)
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
        if self._detect_task:
            self._detect_task.cancel()
            try:
                await self._detect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._detect_task = None
        # rev47: _live_probe_task 已被统一探测器取代,保留取消逻辑处理旧引用(升级/热重启)
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
        # rev15 P0: 定时内存归还(gc.collect + 工作集压缩),解决 Python 分配器不归还 OS 的 RSS 膨胀
        self._memtrim = MemoryTrimmer(int(config.get("memtrim_interval_sec", 600)))
        # rev21: 运行意图持久化 —— start/stop_account 时实时维护,
        # 重启后 startup 按此恢复"关闭之前在跑"的账号(优雅关闭 stop_all 不清 intent)
        self._run_intent_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "run_state.json")
        self._run_intent = self._load_run_intent()
        # rev25: 用户手动点停的账号。自动发作品调度器不会去拉起这些号(避免跟用户操作打架),
        # 用户下次手动启动时自动清除。
        self.human_stopped = set()
        # rev28: 由 server 注入,判断 profile 是否正被远程串流占用(清孤儿时避免误杀)
        self.remote_registry = None
        # rev54: 账号在网状态持久化。掉线判定要跨重启保留 —— 否则每次重启都要
        # 「发包 -> 300333 -> 开浏览器 -> 未登录」把每个掉线账号再白跑一轮。
        self._auth_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "auth_state.json")
        self._auth_offline = self._load_auth_offline()

    # ---------- rev28: 孤儿进程清理 ----------
    _LOGIN_TERMINAL = ("finalized", "success", "failed", "cancelled")

    def _has_active_login(self, account_id):
        """该账号是否有一个还没到终态的扫码会话(说明 profile 正被它占用)。"""
        for s in list((self.login_sessions or {}).values()):
            acc = getattr(s, "account", None) or {}
            if acc.get("id") == account_id and \
                    getattr(s, "status", "") not in self._LOGIN_TERMINAL:
                return True
        return False

    def _profile_busy(self, account_id):
        """该账号 profile 是否正被已知的活会话占用:worker context / 扫码 / 远程串流。

        清孤儿是「按 profile_dir 全杀」,所以必须先确认没有自己的合法 context 在用,
        否则会把正在抓取或正在串流的浏览器一起杀掉。
        """
        w = self.workers.get(account_id)
        if w is not None and getattr(w, "context", None) is not None:
            return True
        if self._has_active_login(account_id):
            return True
        reg = getattr(self, "remote_registry", None)
        if reg is not None:
            try:
                if reg.get(account_id) is not None:
                    return True
            except Exception:
                pass
        return False

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

    async def select_login(self, sid, index=0):
        """前端选择账号(选择页出现时回调)。"""
        s = self.login_sessions.get(sid)
        if not s:
            return False
        return await s.select_account(index)

    async def retry_login_clean(self, sid):
        """锁定冲突(扫了非锁定微信)确认后:重新进入全新登录环境等待扫码。

        登录态在检测到冲突时已经清空,这里只原地重启扫码流程(sid 不变,
        自动重登队列的 active 状态不受影响)。
        """
        s = self.login_sessions.get(sid)
        if not s:
            return None
        if not await s.relaunch_clean():
            return None
        return s

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
    async def start(self, headless=True, auto_relogin=True):
        await self._ensure_playwright()
        self._running = True
        # rev15 P0: 启动定时内存归还(独立于账号,引擎运行期间常驻)
        self._memtrim.start()
        # 评论抓取全局串行锁:多账号经此锁 + _cycle_offset 错峰,形成"间隔排队",
        # 任意时刻最多 1 个账号在抓,浏览器不并发常驻(省内存)。
        self._fetch_lock = asyncio.Lock()
        interval = self.config.get("fetch_interval_sec", 600)
        total = max(1, len(self.config.get("accounts", [])))
        live_idx = 0
        expired = []
        for acc in self.config.get("accounts", []):
            # rev54: 上次已判掉线且登录态没变 -> 不启动(省一次 Chromium 启停),
            # 直接进下面的 expired 列表去排队扫码
            if self._is_known_offline(acc):
                logger.info(f"账号 {acc['id']} 上次判定登录失效且登录态未更新,跳过启动")
                expired.append(acc["id"])
                continue
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
        # 失效账号:后端自动依次推网页二维码,无需用户手动点启动。
        # rev17: 软重启时传 auto_relogin=False —— 自动重启不应擅自开始扫码打扰用户,
        # 未登录账号保持原状,仍可由用户在界面上手动启动。
        if auto_relogin:
            for acc_id in expired:
                await self.enqueue_auto_relogin(acc_id)

    async def stop(self):
        self._running = False
        # rev15 P0: 停掉定时内存归还
        self._memtrim.stop()
        # 并发关闭各 worker:多账号串行关带 FLV 流的 context 很慢,
        # 易触发 graceful_shutdown 的 5s 超时;return_exceptions 保证
        # 单个 worker 失败不中断其余(每个 w.stop 内部已吞 context.close 异常)。
        workers = list(self.workers.values())
        if workers:
            await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
        self.workers.clear()
        await self._emit("engine_status", self.status_snapshot())

    async def add_account(self, account, headless=True):
        # rev28: launch 之前清掉锁死 profile 的孤儿 Chromium。有残留进程占着
        # Cookies / LocalStorage 时,新实例既写不进也读不到登录态,扫多少次码都白扫。
        if not self._profile_busy(account["id"]):
            try:
                reap_profile_orphans(account.get("profile_dir"),
                                     log_tag=f"[{account['id']}] 启动前",
                                     min_age_sec=5)
            except Exception as e:
                logger.warning(f"[{account['id']}] 启动前清理孤儿进程失败: {e}")
        worker = AccountWorker(account, self.storage, self.config, self._emit)
        try:
            await worker.start(self._playwright, headless=headless)
        except Exception:
            # start 抛异常(launch_stealth 失败/goto 超时)时 context 可能已开,需清理避免进程残留
            await worker.stop()
            raise
        worker.on_auth_change = self._on_worker_auth
        self.workers[account["id"]] = worker
        return worker

    async def _auth_offline_on_start(self, account_id, acc):
        """跳过启动时补记一次离线状态并排队扫码(rev54)。"""
        self._auth_offline[account_id] = {
            "reason": (self._auth_offline.get(account_id) or {}).get("reason", "登录态失效"),
            "ts": (self._auth_offline.get(account_id) or {}).get("ts", time.time()),
            "fp": auth_fingerprint(acc),
        }
        self._save_auth_offline()
        try:
            await self.enqueue_auto_relogin(account_id)
        except Exception as e:
            logger.warning(f"[{account_id}] 排队扫码失败: {e}")

    async def start_account(self, account_id):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        await self._ensure_playwright()
        if account_id in self.workers:
            return self.workers[account_id]
        # rev54: 上次已判掉线,并且登录态没变过 -> 别再开一次浏览器去验,直接排队扫码。
        # 扫码成功后 storage_state 会变,指纹不匹配,这条分支自然失效。
        if self._is_known_offline(acc):
            logger.info(f"[{account_id}] 上次判定登录失效且登录态未更新,跳过启动,直接排队扫码")
            await self._auth_offline_on_start(account_id, acc)
            return None
        if self._fetch_lock is None:
            self._fetch_lock = asyncio.Lock()
        w = AccountWorker(acc, self.storage, self.config, self._emit)
        w.on_auth_change = self._on_worker_auth
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
            self.human_stopped.discard(account_id)  # rev25: 手动启动 -> 恢复自动发布资格
            # rev21: 记录运行意图(重启后自动恢复)
            self._run_intent.add(account_id)
            self._save_run_intent()
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

    async def start_relogin(self, account_id, auto=False, on_finished=None, headed=False):
        """已存在账号重新扫码登录:复用原 profile_dir,成功后更新原账号字段。

        rev40: 默认 headless + 网页二维码推送(本机也不再弹窗口);
        只有显式 headed=True(前端点「本机窗口」或配置 login_popup_window)才弹浏览器。

        auto=True 用于失效账号自动依次扫码:on_finished 在终态回调以推进队列。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        if account_id in self.workers:
            await self.stop_account(account_id)
        # rev28: 停掉 worker 后立刻清孤儿,给扫码一个真正干净的 profile
        try:
            reap_profile_orphans(acc.get("profile_dir"), log_tag=f"[{account_id}] 扫码前",
                                 min_age_sec=5)
        except Exception as e:
            logger.warning(f"[{account_id}] 扫码前清理孤儿进程失败: {e}")
        if on_finished is None:
            # rev28: 手动扫码成功后自动把账号拉起来。以前手动模式不给回调,扫码到终态
            # 后没人 start_account,而上面已经把 worker 停掉了 —— 用户看到的是
            # 「扫码成功但账号仍然未登录」,还得自己再点一次启动。
            def on_finished(sid, status, aid):
                asyncio.create_task(self._on_relogin_finished(aid, status, sid))
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit, account=acc,
                            auto_finalize=auto, on_finished=on_finished)
        self.login_sessions[sess.sid] = sess
        await sess.start(headed=headed)
        return sess

    # ---------- 失效账号自动依次扫码 ----------
    async def enqueue_auto_relogin(self, account_id):
        """失效账号入队自动 relogin(网页二维码依次扫);无活跃项时立即开始下一个。"""
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

    async def _on_relogin_finished(self, account_id, status, sid=None):
        if sid:
            self.login_sessions.pop(sid, None)
        elif self._relogin_active_sid:
            self.login_sessions.pop(self._relogin_active_sid, None)
        if status == "finalized":
            # rev54: 扫码成功 = 登录态已经换新,掉线记录作废
            self._clear_auth_offline(account_id)
            # rev29: 等登录会话把 profile 彻底释放再启动。以前立刻 start,和会话收尾的
            # 孤儿清理并发,刚 launch 的浏览器被 reap 当孤儿杀掉(Target closed)。
            await asyncio.sleep(1.5)
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"relogin 后启动失败 {account_id}: {e}")
        # rev28: 只有「当前活跃的那个自动 relogin」结束才推进队列。
        # 手动扫另一个号结束时也走这个回调,无条件清会把自动队列的状态抹掉。
        if self._relogin_active_acc == account_id:
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
        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)
        self._run_intent.discard(account_id)
        # rev25: 记下「人工停止」,自动发作品调度器不要替他拉起
        self.human_stopped.add(account_id)
        self._save_run_intent()
        w = self.workers.pop(account_id, None)
        if w:
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())

    async def logout_account(self, account_id):
        """手动退出登录态(rev22):停 worker / 关 headed 浏览器 -> 清 profile 内登录文件。

        与「删除账号」的区别:账号配置(名称/ID/profile_dir)全部保留,只清登录 Cookie
        与 LocalStorage,下次必须重新扫码。_aid / _log_finder_id / locked_* 一律保留 ——
        重新扫码仍能命中同一账号,锁定校验也照旧生效。
        返回 (ok, 中文说明)。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False, "账号不存在"
        # 1) 先停 worker 释放 profile 目录锁,否则清登录态必然失败
        if account_id in self.workers:
            try:
                await self.stop_account(account_id)
            except Exception as e:
                logger.warning(f"[{account_id}] 退出登录前停止 worker 失败: {e}")
        # 2) 关掉用户手动打开的 headed 浏览器(否则同样占着 profile)
        ctx = self._open_browsers.pop(account_id, None)
        if ctx:
            try:
                await close_context_safely(ctx, acc.get("profile_dir"), f"[{account_id}] logout")
            except Exception as e:
                logger.debug(f"[{account_id}] 退出登录时关闭 headed 浏览器异常(忽略): {e}")
        # 3) 清 profile 内登录文件(函数自带 ./profiles 范围校验),稍等句柄释放
        await asyncio.sleep(0.3)
        ok, msg = clear_login_state(acc.get("profile_dir") or "")
        # 4) 清登录时间(倒计时归零),另记退出时间供前端展示「未登录」
        acc["login_at"] = None
        acc["logout_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_config()
        await self._emit("accounts_updated", {"reason": "logout", "account_id": account_id})
        logger.info(f"[{account_id}] 已退出登录态: {msg}")
        return ok, msg

    # ---------- rev54: 账号在网状态(掉线记录)持久化 ----------
    def _load_auth_offline(self) -> dict:
        """读取上次判定掉线的账号表 {acc_id: {reason, ts, fp}};损坏就返回空表。"""
        try:
            with open(self._auth_file, encoding="utf-8") as f:
                d = json.load(f) or {}
            return {k: v for k, v in d.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _save_auth_offline(self):
        try:
            os.makedirs(os.path.dirname(self._auth_file), exist_ok=True)
            tmp = self._auth_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._auth_offline, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._auth_file)
        except Exception as e:
            logger.warning(f"在网状态落盘失败(忽略): {e}")

    def _clear_auth_offline(self, account_id):
        if self._auth_offline.pop(account_id, None) is not None:
            self._save_auth_offline()

    def _is_known_offline(self, acc) -> bool:
        """该账号上次已判掉线,且登录态一个字节都没变 -> 不必再启动去白验一次。"""
        off = self._auth_offline.get(acc.get("id") or "")
        if not off:
            return False
        return off.get("fp") == auth_fingerprint(acc)

    async def _queue_relogin_after_offline(self, account_id):
        try:
            await self.enqueue_auto_relogin(account_id)
        except Exception as e:
            logger.warning(f"[{account_id}] 掉线后排队扫码失败: {e}")

    async def _on_worker_auth(self, account_id, state, reason=""):
        """worker 在网状态变化:离线 -> 落盘 + 排队扫码;恢复 -> 清记录。"""
        if state == "offline":
            acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
            self._auth_offline[account_id] = {
                "reason": reason or "",
                "ts": time.time(),
                "fp": auth_fingerprint(acc) if acc else "",
            }
            self._save_auth_offline()
            await self._emit("engine_status", self.status_snapshot())
            # 自动推网页二维码:与启动时未登录账号走同一条串行队列,不会同时弹多个。
            # 用 create_task:扫码要 launch 浏览器(几秒),别让它卡在调用方持有的抓取锁里。
            asyncio.create_task(self._queue_relogin_after_offline(account_id))
        elif state == "ok":
            if self._auth_offline.pop(account_id, None) is not None:
                self._save_auth_offline()
                await self._emit("engine_status", self.status_snapshot())

    # ---------- rev21: 运行意图持久化(重启恢复) ----------
    def _load_run_intent(self) -> set:
        """读取上次退出前的运行中账号集合;文件不存在/损坏返回空集。"""
        try:
            with open(self._run_intent_file, encoding="utf-8") as f:
                d = json.load(f)
            return set(d.get("running", []) or [])
        except Exception:
            return set()

    def _save_run_intent(self):
        """运行意图原子落盘(临时文件 + os.replace);失败仅警告不阻断业务。"""
        try:
            os.makedirs(os.path.dirname(self._run_intent_file), exist_ok=True)
            tmp = self._run_intent_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"running": sorted(self._run_intent),
                           "saved_at": time.time()}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._run_intent_file)
        except Exception as e:
            logger.warning(f"运行意图落盘失败(忽略): {e}")

    async def heal_login_profiles(self):
        """启动自愈:profile_dir 还指向扫码临时目录(_login_*)的账号搬回正常目录(rev32)。

        为什么要做:relogin 的 `_move_profile_to` 在目标被占用时会降级返回临时目录,
        并把临时路径写进 config。而 `_login_*` 目录是会被清掉的 —— 清掉之后账号就
        永久登录不上(线上 9/10 号就是这么废的)。

        必须在「账号被自动恢复拉起之前」执行:那时 profile 还空着,才能安全搬目录。
        返回 (搬迁数, 重置数)。
        """
        import shutil
        healed = repaired = 0
        for acc in self.config.get("accounts", []):
            pd = (acc.get("profile_dir") or "").replace("\\", "/")
            if "_login_" not in pd:
                continue
            aid = acc["id"]
            dst = f"./profiles/{aid}"
            if not os.path.isdir(pd):
                # 临时目录已经被清理 -> 登录态彻底没了,重置到正常路径,等用户重新扫码
                logger.warning(f"[自愈] {aid} 的 profile_dir 指向已消失的临时目录 {pd},"
                               f"登录态已丢失,重置为 {dst}(需要重新扫码)")
                try:
                    os.makedirs(dst, exist_ok=True)
                except Exception as e:
                    logger.warning(f"[自愈] {aid} 创建目录失败: {e}")
                    continue
                acc["profile_dir"] = dst
                acc.pop("storage_state", None)
                repaired += 1
                continue
            if os.path.isdir(dst):
                # 目标已存在(旧的失效 profile):清掉占用再替换,临时目录里才是最新登录态
                try:
                    reap_profile_orphans(dst, log_tag=f"[自愈:{aid}]", min_age_sec=5)
                except Exception:
                    pass
                shutil.rmtree(dst, ignore_errors=True)
                if os.path.isdir(dst):
                    logger.warning(f"[自愈] {aid} 目标 profile 被占用,本次跳过({dst})")
                    continue
            try:
                os.rename(pd, dst)
            except Exception as e:
                logger.warning(f"[自愈] {aid} 搬迁失败({pd} -> {dst}): {e}")
                continue
            acc["profile_dir"] = dst
            healed += 1
            logger.info(f"[自愈] {aid} profile 已从 {pd} 搬到 {dst}")
        if healed or repaired:
            self._save_config()
            logger.info(f"[自愈] 完成: 搬迁 {healed} 个, 重置 {repaired} 个(需重新扫码)")
        else:
            logger.info("[自愈] 没有指向临时目录的账号 profile,无需处理")
        return healed, repaired

    async def restore_running_accounts(self, gap_sec: float = 4.0):
        """重启后恢复关闭前在跑的账号。

        - intent 由 start_account/stop_account 实时维护:用户关窗口/重启/强杀都不会丢,
          只有用户主动点"停止"才清除 —— 语义即"关闭之前在运行中";
        - 只恢复仍存在于 config 的账号(已删除的顺手清出 intent);
        - 逐个错峰启动(默认间隔 4s),避免同时拉起多个 Chromium 争抢资源;
        - 未登录/启动失败的账号移出 intent,避免每次重启空开浏览器。
        """
        known = {a["id"] for a in self.config.get("accounts", [])}
        stale = self._run_intent - known
        if stale:
            self._run_intent -= stale
            self._save_run_intent()
        todo = [aid for aid in sorted(self._run_intent) if aid not in self.workers]
        if not todo:
            logger.info("[恢复] 无需要自动恢复的账号")
            return
        logger.info(f"[恢复] 重启后自动恢复 {len(todo)} 个关闭前在跑的账号: {todo}")
        for i, aid in enumerate(todo):
            if i:
                await asyncio.sleep(gap_sec)
            try:
                await self.start_account(aid)
                if aid not in self.workers:
                    # 未登录/启动失败:移出 intent,避免下次重启再空试
                    self._run_intent.discard(aid)
                    self._save_run_intent()
                    logger.warning(f"[恢复] {aid} 未登录或启动失败,已移出恢复列表")
            except Exception as e:
                logger.warning(f"[恢复] {aid} 启动异常(保留 intent,下次重启再试): {e}")
        logger.info("[恢复] 自动恢复流程结束")

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
        """全部账号立即抓取一次。

        rev52: 判定条件不能只看 logged_in —— 纯 HTTP 模式下根本没开浏览器,
        logged_in 还是初始的 False,会导致「立即抓取」把所有 HTTP 可用账号跳掉。
        """
        def _usable(w):
            if getattr(w, "auth_state", "") == "offline":
                return False
            if w.logged_in:
                return True
            try:
                return bool(w.api is not None and w.api.http_available())
            except Exception:
                return False

        tasks = [w.fetch_once() for w in self.workers.values() if _usable(w)]
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

    def set_account_lock(self, account_id, locked):
        """锁定/解锁账号的微信身份。

        锁定:把当前账号的微信标识(_log_finder_id + name)存为 locked_*,
        之后该账号只接受这个微信登录;解锁:清除 locked_* 恢复自由登录。
        """
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        if locked:
            fid = (acc.get("_log_finder_id") or "").strip()
            nm = (acc.get("name") or "").strip()
            if not fid and not nm:
                return False  # 尚无可用微信标识(未登录过),无法锁定
            acc["locked_finder_id"] = fid
            acc["locked_name"] = nm
        else:
            acc.pop("locked_finder_id", None)
            acc.pop("locked_name", None)
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
            "login_at": acc.get("login_at"),
            "logout_at": acc.get("logout_at"),
            "locked": bool(acc.get("locked_finder_id") or acc.get("locked_name")),
            "locked_name": acc.get("locked_name", ""),
            # rev48: 最近一次直播探测的结果。以前想知道"刚才到底探了没、走的哪条路"
            # 只能翻母本 17MB 的 GBK 日志,现在卡片上就能看。
            "probe": self._probe_view(w),
            # rev54: 在网状态。offline = 已判登录失效,该账号不再发任何请求,
            # 前端可以据此把卡片标成"待扫码"而不是"已登录"。
            "auth_state": getattr(w, "auth_state", "unknown") if w else "unknown",
            "auth_reason": getattr(w, "auth_reason", "") if w else "",
            "auth_offline_at": (getattr(w, "auth_ts", None)
                                if w and getattr(w, "auth_state", "") == "offline" else None),
        }

    @staticmethod
    def _probe_view(w):
        """rev48: 探测状态视图。via=http 表示这轮是纯 HTTP(没起浏览器)。"""
        p = (getattr(w, "_last_probe", None) or {}) if w else {}
        why = (getattr(w, "_http_last_why", "") or "") if w else ""
        if not p and not why:
            return None
        return {
            "ts": p.get("ts"),
            "live": p.get("live"),
            "reason": p.get("reason"),
            "status": p.get("status"),
            "via": p.get("via"),
            "borrowed_spec": p.get("borrowed_spec"),
            "started_browser": bool(p.get("started_browser")),
            "why_unavailable": why,
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
