"""直播大屏抓取: 实测验证方案(2026-07-29 rev6)。

通过诊断脚本实测确认: check_live_status 响应体含 liveObjectId + audiencePlayUrl(.flv 流),
get_live_info 响应体含 liveStats。本版据此实现,不再依赖猜测的接口字段。

内存安全:
  旧版 page.route("**/*") 对每个请求(图片/字体/CSS/FLV 分片/轮询)创建 Python 代理对象,
  长跑直播下 Playwright 不回收(microsoft/playwright#20765),累积到 5-7GB。
  本版:
  - CDP Network.setBlockedURLs: 浏览器层阻断图片/字体/CSS/媒体(0 Python 对象)
  - page.on("response"): 被动捕获 get_live_info / check_live_status 响应体(实测可靠,无泄漏)
  - page.route("**/*.flv*") / ("**/*.m3u8*"): 仅 flv/m3u8 两条精确路由,捕获 stream_url + abort
    中止下载(等价原 page.route abort,但非 **/* 海量路由,无累积泄漏)

实测验证(diagnose_live.py, uspoloassn7):
  - check_live_status 返回 data.audiencePlayUrl = "//pull-m1.wxlivecdn.com/.../orig_..._ns405.flv?..."
  - 仅用 page.on("response") 即可完整读到,无需 CDP Fetch 域(CDP Fetch 反而干扰响应事件)
"""
import asyncio
import json
import re
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

# CDP 浏览器层阻断: 非必要资源(图片/字体/CSS/媒体)。
# 注意: FLV 直播流【不】在此阻断! setBlockedURLs 会在 requestWillBeSent(即 page.on("request"))
# 之前就拦掉请求,导致抓不到 FLV URL。FLV 改由 page.route("**/*.flv*") 精确拦截。
_BLOCKED_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.ico", "*.bmp",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.css",
    "*.mp4", "*.webm", "*.mp3", "*.wav", "*.ogg", "*.m4a",
]


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
        self._flv_req_warned = False
        # CDP session(仅用于浏览器层阻断非必要资源,不取 body,无代理对象)
        self._cdp = None

    async def goto_live(self):
        """设置 CDP 阻断,注册 page.on 捕获回调 + flv 精确路由,然后导航到 liveBuild 页。"""
        await self._setup_cdp()
        self.page.on("response", self._on_resp)
        await self._install_flv_route()
        try:
            await self.page.goto(LIVE_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] goto liveBuild 失败: {e}")

    async def _setup_cdp(self):
        """CDP: Network.setBlockedURLs 在浏览器层阻断图片/字体/CSS/媒体(0 Python 对象)。

        不启用 Fetch 域——实测确认 Fetch.enable 会干扰 check_live_status 的响应事件,
        导致 live_object_id 取不到。FLV 用 page.route 精确拦截(见 _install_flv_route)。
        CDP 不可用时降级: 不阻断(响应量会变大,但 page.on 仍能捕获关键 API)。
        """
        try:
            self._cdp = await self.page.context.new_cdp_session(self.page)
            await self._cdp.send("Network.enable")
            await self._cdp.send("Network.setBlockedURLs", {"urls": _BLOCKED_URL_PATTERNS})
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] CDP 初始化失败(降级: 不阻断): {e}")
            self._cdp = None

    async def _install_flv_route(self):
        """page.route 精确拦截 flv/m3u8: 捕获 stream_url 后 abort 中止下载。

        仅 2 条精确路由(**/*.flv* / **/*.m3u8*),非 **/* 海量路由,无 Playwright 代理对象累积泄漏。
        这是原代码能正常出视频的方案,经实测验证最稳。
        """
        try:
            await self.page.route("**/*.flv*", self._handle_flv)
            await self.page.route("**/*.m3u8*", self._handle_flv)
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] flv route 安装失败(降级: 靠 response 兜底): {e}")

    async def _handle_flv(self, route):
        """flv/m3u8 请求: 先抓 URL 作为 stream_url,再 abort 中止下载(后端不缓存流体,
        前端 flv.js 独立拉流)。"""
        u = route.request.url or ""
        if ".flv" in u or ".m3u8" in u or "wxlivecdn" in u or "trtc" in u:
            if u != self.stream_url:
                self.stream_url = u
                logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(来自 route): {u[:80]}...")
        try:
            await route.abort()
        except Exception:
            pass

    # ======================== page.on: 被动捕获响应体 ========================

    async def _on_resp(self, resp):
        u = resp.url or ""
        if "channels.weixin.qq.com" not in u:
            return
        if "get_live_info" in u:
            await self._capture(resp)
        elif "check_live_status" in u:
            await self._capture_live_status(resp)

    async def _capture(self, resp):
        """get_live_info 响应 -> liveStats(当前在线/增值统计) + 兜底 liveObjectId。"""
        try:
            txt = await resp.text()
            data = json.loads(txt)
            d = data.get("data") or {}
            stats = d.get("liveStats")
            if stats:
                self.live_stats = stats
                self.updated_at = datetime.now().isoformat()
                self.updated_at_ts = time.time()
            # liveObjectId 主要从 check_live_status 取;get_live_info 偶尔也返回,作为兜底。
            live_id = d.get("liveObjectId") or (stats or {}).get("liveObjectId")
            if live_id and not self.live_object_id:
                self.live_object_id = str(live_id)
                logger.info(f"[live:{self.account_id}] 拿到 liveObjectId={live_id}(来自 get_live_info),dashboard 抓取启用")
            elif stats and not self.live_object_id and not self._no_liveid_warned:
                self._no_liveid_warned = True
                stats_keys = list(stats.keys()) if isinstance(stats, dict) else type(stats).__name__
                logger.warning(f"[live:{self.account_id}] 在直播但未取到 liveObjectId"
                               f"(get_live_info 无此字段,check_live_status 未捕获),dashboard 5 字段将不显示。"
                               f"liveStats keys={stats_keys}; stream_url={self.stream_url}")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] get_live_info 解析失败: {e}")

    async def _capture_live_status(self, resp):
        """check_live_status 响应 -> liveObjectId(场次 ID,供 dashboard API) + stream_url(.flv 流)。

        这是 stream_url 的【主要可靠来源】(实测确认 audiencePlayUrl 为 // 开头的 .flv URL)。
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
            # .flv 流 URL: 优先 audiencePlayUrl,兜底 liveStreamUrlInfo.liveCdnUrl(均 // 开头 -> 补 https:)
            flv = d.get("audiencePlayUrl") or ((d.get("liveStreamUrlInfo") or {}).get("liveCdnUrl") or "")
            if flv and (".flv" in flv or ".m3u8" in flv):
                url = flv if flv.startswith("http") else "https:" + flv
                if url != self.stream_url:
                    self.stream_url = url
                    logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(来自 check_live_status): {url[:80]}...")
            else:
                # 兜底: 在整个响应体里扫 .flv 流 URL(字段名若改版也能兜住)
                m = re.search(r'https?://[^\s"\'\\<>]*\.flv[^\s"\'\\<>]*', txt)
                if not m:
                    m = re.search(r'//[^\s"\'\\<>]*\.flv[^\s"\'\\<>]*', txt)
                if m:
                    raw = m.group(0)
                    url = raw if raw.startswith("http") else "https:" + raw
                    if url != self.stream_url:
                        self.stream_url = url
                        logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(响应体扫描兜底): {url[:80]}...")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] check_live_status 解析失败: {e}")

    # ======================== 关闭清理 ========================

    async def close(self):
        """关闭前清理: 移除 page 事件回调 + flv 路由 + CDP detach + 断 page 引用。"""
        try:
            self.page.remove_listener("response", self._on_resp)
        except Exception:
            pass
        for pat in ("**/*.flv*", "**/*.m3u8*"):
            try:
                await self.page.unroute(pat)
            except Exception:
                pass
        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
        self.page = None

    # ======================== 静态工具方法 ========================

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
        """扫描响应找疑似场次 ID 候选,返回紧凑字符串供 warn 日志定位字段名/类型。"""
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
            if isinstance(v, int) and v >= 10 ** 15:
                push(p, v)
            elif isinstance(v, str) and v.isdigit() and len(v) >= 15:
                push(p, v)
            low = p.lower()
            if isinstance(v, (str, int)) and any(t in low for t in
                                                  ("objectid", "liveid", "finderid", "liveobj", "live_id",
                                                   "object_id")):
                push(p, v)

        walk(obj, "")
        return ", ".join(cands[:12]) or "(无)"

    # ======================== Dashboard 主动抓取 ========================

    async def _ensure_ids(self):
        """从 liveBuild page localStorage 读 _aid + _log_finder_id。"""
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
        counts = {}
        for item in items:
            dims = item.get("dimensions") or []
            if len(dims) != 1 or str(dims[0].get("type")) != "3":
                continue
            label = dims[0].get("value") or dims[0].get("uxLabel") or ""
            counts[label] = counts.get(label, 0) + sum(
                int(x.get("value", 0) or 0) for x in (item.get("data") or []))
        total = sum(counts.values())
        if total > 0:
            return round(counts.get("男性", 0) / total * 100, 2)
        return None

    async def _fetch_dist(self):
        j = await self._dashboard_post(DISTRIBUTION_CHANNEL_API, {
            "liveObjectId": self.live_object_id, "type": 2,
        })
        if not j:
            return None
        return j.get("data") or {}

    async def _fetch_refund_rate(self):
        j = await self._dashboard_post(EC_DATA_SUMMARY_API, {"liveObjectId": self.live_object_id})
        if not j:
            return None
        return j.get("data") or {}

    async def fetch_dashboard_data(self):
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
        if any(v is not None for v in metrics.values()):
            self._dash_fail_warned = False
        elif not self._dash_fail_warned:
            self._dash_fail_warned = True
            aid_ok = bool(self._aid and self._log_finder_id)
            logger.warning(f"[live:{self.account_id}] dashboard 抓取全失败(liveObjectId={self.live_object_id},"
                           f"_aid/finder_id={'已取' if aid_ok else '缺失'})")
        return metrics

    async def fetch(self):
        """返回当前抓取结果;附带 stream_url 诊断(供日志排查前端无视频时定位)。"""
        if not self.stream_url and not self._flv_req_warned:
            self._flv_req_warned = True
            logger.warning(f"[live:{self.account_id}] stream_url 仍为空(live_object_id={self.live_object_id},"
                           f"检查 check_live_status 是否返回 audiencePlayUrl/liveCdnUrl)")
        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
            "metrics": self._dashboard_cache or {},
        }
