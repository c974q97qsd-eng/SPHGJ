"""直播大屏抓取:被动拦截 liveBuild 页自身请求 + 主动同源 POST dashboard API。不抓 DOM。

被动拦截(liveBuild 页自身发起, SPHGJ 不主动请求, 无额外开销):
- get_live_info 响应(~5s/次) -> liveStats: overlay 当前在线、增值统计(累计观看/成交)、is_live
- check_live_status 响应(页面加载时) -> liveObjectId(场次 ID; get_live_info 不返回)
- .flv 请求(pull-m1.wxlivecdn.com/.../orig.flv) -> stream_url, 前端 flv.js 播放

主动同源 POST(在 liveBuild page fetch, 补 get_live_info 不提供的 5 字段):
- get_ec_conversion_dashboard_data_v3 / getLiveDistributionChannel / get_live_ec_data_summary
  -> natural_traffic/natural_gmv/refund_rate/male_ratio/heat_gmv_per_1000(见 fetch_dashboard_data)
  需 liveObjectId(来自 check_live_status)。无需进 dashboardV4 页。
"""
import asyncio
import json
import time
import logging
from datetime import datetime
from .selectors import LIVE_URL
from .metrics import extract_all

logger = logging.getLogger("sphgj")

# dashboardV4 数据 API(同源 POST,在 liveBuild page fetch)
DASHBOARD_DATA_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/statistic/get_ec_conversion_dashboard_data_v3"
DISTRIBUTION_CHANNEL_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/svrkit/MmFinderECAssistantDataSvr/getLiveDistributionChannel"
EC_DATA_SUMMARY_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/statistic/get_live_ec_data_summary"
DASHBOARD_PAGE_URL_ENC = "https%3A%2F%2Fchannels.weixin.qq.com%2Fmicro%2Fstatistic%2FdashboardV4"

# 渠道大类:newLiveDstChannelType 1=公域 / 2=加热 / 4=私域
PUBLIC_CHANNEL_TYPE = 1
DASHBOARD_INTERVAL = 60  # dashboard 数据抓取间隔(秒);实际由 account_manager 读 config.dashboard_interval_sec 控制


class LiveFetcher:
    def __init__(self, page, account_id):
        self.page = page
        self.account_id = account_id
        self.live_stats = None
        self.stream_url = None
        self.updated_at = None
        self.updated_at_ts = 0.0  # 最近一次拿到 liveStats 的 timestamp,用于判断 is_live
        # dashboard 抓取
        self.live_object_id = None  # 直播场次 ID(从 check_live_status 响应取;get_live_info 不返回)
        self._aid = None            # 会话 _aid(localStorage __ml::aid)
        self._log_finder_id = None  # 账号唯一标识(localStorage finder_username)
        self._dashboard_cache = {}  # {metric_key: value},由 fetch_dashboard_data 写入
        self._dashboard_ts = 0.0
        self._no_liveid_warned = False   # 在直播但拿不到 liveObjectId 时 warn 一次
        self._dash_fail_warned = False   # dashboard 抓取全失败时 warn 一次(成功后重置)
        # 持有 fire-and-forget task 引用,防止 task 被 GC 中途取消 + 关 page 时统一 cancel。
        # 否则 task 持有 resp -> resp 持有 page -> page 回调表持有 LiveFetcher,
        # 挂起的 task(resp.text() 未返回)会卡住整条链,page/LiveFetcher 不被回收,
        # P0-1 关/重开 live_page 循环下累积成内存泄漏。
        self._pending_tasks: set = set()
        page.on("request", self._on_req)
        page.on("response", self._on_resp)
        # A: route 阻断非必要资源--后端 headless 不渲染画面/不播放流,只需拦截 API 响应。
        # abort FLV 流(记录 URL 给前端 flv.js 后不下载流体,省直播中最大内存头)+
        # image/font/stylesheet/media,大幅降低 liveBuild 页常驻内存。前端用 stream_url
        # 在 WebView2 独立拉流播放,与后端 abort 互不影响。
        self._route_handler = None
        self._install_route(page)

    def _install_route(self, page):
        async def handler(route):
            try:
                req = route.request
                u = req.url or ""
                rt = req.resource_type
                # FLV 直播流:先记 URL(前端 flv.js 用),再 abort 不下载流体
                if ".flv" in u and ("wxlivecdn" in u or "trtc" in u):
                    if u != self.stream_url:
                        self.stream_url = u
                    await route.abort()
                    return
                # 非必要资源 abort(不影响 API:xhr/fetch/document/script 照常)
                if rt in ("image", "font", "stylesheet", "media"):
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                # route 异常时尽量放行,避免阻断正常 API
                try:
                    await route.continue_()
                except Exception:
                    pass
        try:
            page.route("**/*", handler)
            self._route_handler = handler
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] page.route 安装失败(资源阻断未生效): {e}")

    def _on_req(self, req):
        u = req.url or ""
        # 直播 FLV 流(pull-m1.wxlivecdn.com / trtc)
        if ".flv" in u and ("wxlivecdn" in u or "trtc" in u):
            self.stream_url = u

    def _on_resp(self, resp):
        u = resp.url or ""
        if "channels.weixin.qq.com" not in u:
            return
        if "get_live_info" in u:
            self._spawn(self._capture(resp), "get_live_info")
        elif "check_live_status" in u:
            self._spawn(self._capture_live_status(resp), "check_live_status")

    def _spawn(self, coro, tag):
        """创建 fire-and-forget task 并持有引用:加入 _pending_tasks,完成时自动移除。

        不持有引用的 task 会被 GC 中途取消;更危险的是挂起的 task 持有 resp->page->LiveFetcher
        链,导致关 page 后 LiveFetcher 不被回收(P0-1 关/重开循环下累积泄漏)。
        """
        try:
            t = asyncio.create_task(coro)
            self._pending_tasks.add(t)
            t.add_done_callback(self._pending_tasks.discard)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 调度 {tag} 抓取失败: {e}")
            coro.close()  # 没成功创建 task,关掉 coro 避免未消费协程警告

    async def close(self):
        """关闭前清理:移除 page 事件回调 + 取消挂起 task + 断开 page 引用。

        必须在 page.close() 前调用,否则 page 回调表 + 挂起 task 会持有 LiveFetcher
        与 page 对象,阻止 GC(下播后 P0-1 关/重开循环下累积成内存增长)。
        """
        # 1. 移除回调(try/except:page 已关时 remove_listener 可能抛)
        for evt, fn in (("request", self._on_req), ("response", self._on_resp)):
            try:
                self.page.remove_listener(evt, fn)
            except Exception:
                pass
        # 1b. 移除 route handler(A: 资源阻断)
        if self._route_handler:
            try:
                self.page.unroute("**/*", self._route_handler)
            except Exception:
                pass
            self._route_handler = None
        # 2. 取消所有挂起 task(resp.text() 未返回的会卡住 page 不释放)
        for t in list(self._pending_tasks):
            if not t.done():
                t.cancel()
        self._pending_tasks.clear()
        # 3. 断 LiveFetcher -> page 强引用
        self.page = None

    async def _capture(self, resp):
        try:
            txt = await resp.text()
            data = json.loads(txt)
            d = data.get("data") or {}
            stats = d.get("liveStats")
            if stats:
                self.live_stats = stats
                self.updated_at = datetime.now().isoformat()
                self.updated_at_ts = time.time()
            # liveObjectId 主要从 check_live_status 取(见 _capture_live_status);get_live_info 不返回该字段。
            # 这里仅在 get_live_info 偶尔返回时刷新作为兜底;不再因取不到而 warn(否则必然误报)。
            live_id = d.get("liveObjectId") or (stats or {}).get("liveObjectId")
            if live_id and not self.live_object_id:
                self.live_object_id = str(live_id)
                logger.info(f"[live:{self.account_id}] 拿到 liveObjectId={live_id}(来自 get_live_info),dashboard 抓取启用")
            elif stats and not self.live_object_id and not self._no_liveid_warned:
                # 在直播但 check_live_status 尚未捕获 liveObjectId -> dashboard 5 字段无法抓,前端显示 "-"/0。
                # 正常情况 check_live_status 页面加载时即给出;持续不出现才 warn 一次供定位。
                self._no_liveid_warned = True
                stats_keys = list(stats.keys()) if isinstance(stats, dict) else type(stats).__name__
                logger.warning(f"[live:{self.account_id}] 在直播但未取到 liveObjectId"
                               f"(get_live_info 无此字段,check_live_status 未捕获),dashboard 5 字段将不显示。"
                               f"liveStats keys={stats_keys}; stream_url={self.stream_url}")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] get_live_info 解析失败: {e}")

    async def _capture_live_status(self, resp):
        """从 check_live_status 响应取 liveObjectId(场次 ID,供 dashboard API)+ stream_url(.flv 流)。

        check_live_status 在 liveBuild 页面加载时调用(非周期,约连调 2 次),
        liveObjectId 一场直播不变,取一次即可;account_manager ~48min reload 会重新触发刷新。
        get_live_info 不返回 liveObjectId,故 liveObjectId 必须从此接口取。
        stream_url 优先取 audiencePlayUrl(flv.js 观众视角),兜底 liveStreamUrlInfo.liveCdnUrl
        (均 // 开头,补 https:);_on_req 拦截 .flv 请求的方式保留为兜底/刷新 token。
        """
        try:
            txt = await resp.text()
            data = json.loads(txt)
            d = data.get("data") or {}
            live_id = d.get("liveObjectId") or self._find_live_object_id(d)
            if live_id:
                if not self.live_object_id:
                    logger.info(f"[live:{self.account_id}] 拿到 liveObjectId={live_id}(来自 check_live_status),dashboard 抓取启用")
                self.live_object_id = str(live_id)
            # .flv 流 URL:优先 audiencePlayUrl,兜底 liveStreamUrlInfo.liveCdnUrl(均 // 开头 -> 补 https:)
            flv = d.get("audiencePlayUrl") or ((d.get("liveStreamUrlInfo") or {}).get("liveCdnUrl") or "")
            if flv and ".flv" in flv:
                url = flv if flv.startswith("http") else "https:" + flv
                if url != self.stream_url:
                    self.stream_url = url
                    logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(来自 check_live_status)")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] check_live_status 解析失败: {e}")

    @staticmethod
    def _find_live_object_id(obj):
        """遍历响应找 18+ 位纯数字字符串(直播场次 ID)兜底。"""
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, str) and v.isdigit() and len(v) >= 18:
                    return v
                r = LiveFetcher._find_live_object_id(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = LiveFetcher._find_live_object_id(v)
                if r:
                    return r
        return None

    @staticmethod
    def _diagnose_id_candidates(obj):
        """扫描响应找疑似场次 ID 候选,返回紧凑字符串供 warn 日志定位字段名/类型。
        收集:>=15 位纯数字串、>=10^15 的 int、路径名含 objectid/liveid/finderid 的字段值。
        阈值比 _find_live_object_id 低(15 位),用于诊断字段名是否变更或 ID 长度不同。"""
        cands = []
        seen = set()

        def push(path, val):
            s = str(val)[:32]
            if (path, s) in seen:
                return
            seen.add((path, s))
            cands.append(f"{path}={s}")

        def walk(o, path):
            if isinstance(o, dict):
                for k, v in o.items():
                    p = f"{path}.{k}" if path else k
                    _try(p, v)
                    walk(v, p)
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{path}[{i}]")

        def _try(p, v):
            if isinstance(v, bool):
                return
            if isinstance(v, int) and v >= 10**15:
                push(p, v)
            elif isinstance(v, str) and v.isdigit() and len(v) >= 15:
                push(p, v)
            low = p.lower()
            if isinstance(v, (str, int)) and any(t in low for t in
                    ("objectid", "liveid", "finderid", "liveobj", "live_id", "object_id")):
                push(p, v)

        walk(obj, "")
        return ", ".join(cands[:12]) or "(无)"

    async def _ensure_ids(self):
        """从 liveBuild page localStorage 读 _aid + _log_finder_id(同源可直读)。"""
        if self._aid and self._log_finder_id:
            return
        try:
            ids = await self.page.evaluate("""()=>{
                const raw = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
                const fid = localStorage.getItem('finder_username') || '';
                const unquote = s => s ? s.replace(/^"|"$/g, '') : '';
                return {aid: unquote(raw), fid: unquote(fid)};
            }""")
            self._aid = self._aid or ids.get("aid")
            self._log_finder_id = self._log_finder_id or ids.get("fid")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 读取 localStorage 失败: {e}")

    async def _dashboard_post(self, url, body):
        """在 liveBuild page 同源 fetch dashboard API(POST,cookie 自动带)。"""
        await self._ensure_ids()
        if not self._aid or not self._log_finder_id:
            return None
        full_url = f"{url}?_aid={self._aid}&_pageUrl={DASHBOARD_PAGE_URL_ENC}"
        payload = {
            "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "",
            "_log_finder_id": self._log_finder_id,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
            **body,
        }
        js = """async (args) => {
            const r = await fetch(args.url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/json'}, body: JSON.stringify(args.body)});
            return await r.text();
        }"""
        txt = await self.page.evaluate(js, {"url": full_url, "body": payload})
        return json.loads(txt)

    async def _fetch_conv(self):
        """get_ec_conversion_dashboard_data_v3,返回 data dict(含 overview/conversionAnalysis/
        trendingSource/portraitAudience)。具体指标提取见 metrics.extract_all。"""
        j = await self._dashboard_post(DASHBOARD_DATA_API, {
            "liveObjectId": self.live_object_id,
            "panelTrendingSourceQueryOption": {
                "isEnabled": True, "timeRange": 2,
                "enabledMetricTypes": [15], "enabledTrafficType": True,
                "enabledPromoteType": True,
            },
            "panelPortraitAudienceQueryOption": {
                "isEnabled": True,
                "enabledDimensionTypes": [2, 3, 4, 5, 9, 10, 12, 13, 14, 1],
                "enabledMetricTypes": [2, 1],
                "enabledDimensionPrefectureLevelAdcode": False,
                "selectedProvinceLevelAdcode": "",
                "enabledFollowerCumulativeWatchUv": False,
            },
        })
        if not j:
            return None
        return j.get("data") or {}

    @staticmethod
    def _calc_male_ratio(items):
        """portraitAudience 性别维度(type 3):男 / (男+女+未知) * 100。"""
        counts = {}
        for item in items:
            dims = item.get("dimensions") or []
            if len(dims) != 1 or str(dims[0].get("type")) != "3":
                continue
            label = dims[0].get("value") or dims[0].get("uxLabel") or ""
            counts[label] = counts.get(label, 0) + sum(int(x.get("value", 0) or 0) for x in (item.get("data") or []))
        total = sum(counts.values())
        if total > 0:
            return round(counts.get("男性", 0) / total * 100, 2)
        return None

    async def _fetch_dist(self):
        """getLiveDistributionChannel,返回 data dict(含 liveDistChannelSourceStats)。"""
        j = await self._dashboard_post(DISTRIBUTION_CHANNEL_API, {
            "liveObjectId": self.live_object_id, "type": 2,
        })
        if not j:
            return None
        return j.get("data") or {}

    async def _fetch_refund_rate(self):
        """get_live_ec_data_summary,返回 data dict(含 totalGmv/refundRate/customerPrice 等)。"""
        j = await self._dashboard_post(EC_DATA_SUMMARY_API, {"liveObjectId": self.live_object_id})
        if not j:
            return None
        return j.get("data") or {}

    async def fetch_dashboard_data(self):
        """并发抓三个 dashboard API,用 metrics.extract_all 提取全部指标 -> {metric_key: value}。

        需 live_object_id(从 check_live_status 取,账号需在直播)。失败字段值 None。
        抓取间隔由 account_manager 读 config.dashboard_interval_sec 控制(默认 60s)。
        """
        if not self.live_object_id:
            return None
        results = await asyncio.gather(
            self._fetch_conv(), self._fetch_dist(), self._fetch_refund_rate(),
            return_exceptions=True)
        conv = dist = summary = None
        for idx, name in enumerate(("conv", "dist", "summary")):
            r = results[idx]
            if isinstance(r, Exception):
                logger.debug(f"[live:{self.account_id}] {name} 抓取失败: {r}")
            elif name == "conv":
                conv = r
            elif name == "dist":
                dist = r
            else:
                summary = r
        metrics = extract_all(conv, dist, summary)
        self._dashboard_cache = metrics
        self._dashboard_ts = time.time()
        # 诊断:全失败 warn 一次(成功后重置,恢复后再失败能再 warn)
        if any(v is not None for v in metrics.values()):
            self._dash_fail_warned = False
        elif not self._dash_fail_warned:
            self._dash_fail_warned = True
            aid_ok = bool(self._aid and self._log_finder_id)
            logger.warning(f"[live:{self.account_id}] dashboard 抓取全失败(liveObjectId={self.live_object_id},"
                           f"_aid/finder_id={'已取' if aid_ok else '缺失'}),卡片指标将不显示")
        return metrics

    async def goto_live(self):
        try:
            await self.page.goto(LIVE_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] goto liveBuild 失败: {e}")

    async def fetch(self):
        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
            "metrics": self._dashboard_cache or {},
        }
