"""直播大屏抓取: 实测验证方案(2026-07-29 rev9) + 阶段化内存精简(rev10~rev11) + 主动轮询(rev13)。

通过诊断脚本实测确认: check_live_status 响应体含 liveObjectId + audiencePlayUrl(.flv 流),
get_live_info 响应体含 liveStats。本版据此实现,不再依赖猜测的接口字段。

内存演进:
  - 旧版 page.route("**/*") 对每个请求创建 Python 代理对象,Playwright 不回收
    (microsoft/playwright#20765),累积 5-7GB —— 已改为 page.route("**/*.flv*") 精确路由。
  - rev6~rev8 在 goto 前用 CDP setBlockedURLs 拦图片/CSS/媒体 -> 拦掉 *.css 导致
    liveBuild SPA 无法初始化,check_live_status 永不触发 -> 误判非直播(rev9 撤销)。
  - rev9 后实测: Python 主进程长跑涨到 9GB,而 4 个浏览器进程仅 ~200MB ——
    泄漏在 Python 驱动层,三个来源:
    1) FLV 路由: 直播页预览播放器被 abort 后不断重试拉流,每次重试创建一组
       Python Route/Request 对象,驱动层不回收(#20765 同类);
    2) 撤销阻断后,直播间弹幕头像等海量图片响应在 Python 侧持续产生 Response 对象;
    3)(rev11 发现) page.on("response") 为【每一个网络响应】创建 Python Response 对象,
       直播间弹幕/观众数轮询/tracking/analytics 每分钟数百个请求,每个 1-5KB 不释放 ->
       这是最大泄漏源(占 11GB 中的大头)。

  rev10 阶段化精简:
    stage1(拿到 liveStats/liveObjectId): CDP setBlockedURLs 拦图片+媒体(不含 css/js/api);
    stage2(拿到 stream_url): 追加拦 FLV + 解除 page.route FLV 路由 + JS 停 <video>。

  rev11 核心修复: 用 CDP Network.responseReceived 替代 page.on("response")。
    CDP 事件是轻量 dict,仅在检测到目标 URL(get_live_info/check_live_status)时才调用
    Network.getResponseBody 读体 —— 其余数千个网络请求【零 Python 对象分配】。

  rev12 基线: rev11 代码(本仓库 rev12 tag)。实测 Python 主进程仍顶在 ~4.7GB:
    Network.responseReceived 虽不建 Response 对象,但 Playwright 仍【为每个响应】向 Python
    派发一个事件 dict(直播间每秒数十~上百响应),4 路直播每秒数千 CDP 事件进 Python 事件循环
    -> 对象剧烈 churn,Python 分配器不把 freed 内存归还 OS,RSS 持续顶高(膨胀非泄漏)。

  rev13 根治(本版): 砍掉常驻响应监听,改【主动轮询】。
    机制:
      1) goto_live 时短暂监听页面自身发出的 get_live_info/check_live_status 请求
         (Network.requestWillBeSent),【动态学到】其完整 url+method+headers+postData;
         两个都学到后立即移除监听 -> 此后稳态直播期间【零常驻网络事件】进 Python。
      2) 每个周期用学到的参数经 page.evaluate(fetch) 同源重放(自动带 cookie)拉取最新数据,
         在 Python 侧只产生一次 fetch 往返的少量对象(对比 rev12 每秒数千事件)。
      3) 若接口要求一次性 token 导致重放失败,连续 3 次失败后【自动回退】到 rev11 的
         CDP 响应捕获(功能不退化,仅回到 rev12 的内存水位)。
    学习期间(requestWillBeSent)的 churn 仅持续数秒(学到即关),远小于 rev12 的常驻 churn。
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

# ======================== rev10: 阶段化内存精简 ========================
# stage1(拿到 liveStats/liveObjectId): 浏览器层拦图片+媒体(不含 css/js/api)
# stage2(拿到 stream_url): 追加拦 FLV + 解除 page.route FLV 路由 + 停 video
_SLIM_MEDIA_URLS = [
    "*.png*", "*.jpg*", "*.jpeg*", "*.gif*", "*.svg*", "*.webp*", "*.ico*", "*.bmp*",
    "*.mp4*", "*.webm*", "*.mp3*", "*.wav*", "*.ogg*", "*.m4a*",
]
_SLIM_FLV_URLS = ["*.flv*", "*.m3u8*", "*wxlivecdn*", "*trtc*"]


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
        # rev10~rev13: 阶段化内存精简状态 + rev13 主动轮询
        self._slim_stage = 0     # 0=未精简 1=已拦图片/媒体 2=已拦FLV并解除route
        self._cdp = None         # 统一 CDP session(精简 setBlockedURLs + 学习 + 回退捕获)
        self._cdp_ready = False  # Network.enable 是否已发送
        self._slim_task = None   # route 内异步触发精简的 task 引用(防 GC)
        # rev13: 主动轮询相关
        self._learned = {}              # {"live_info": spec, "live_status": spec} 动态学到的请求规格
        self._learning = True           # 是否仍在监听页面请求以学习 API
        self._learn_deadline = 0.0      # 学习超时阈值(>此时间未学到则回退 CDP)
        self._poll_fail_streak = 0      # 主动轮询连续失败计数
        self._cdp_fallback = False      # 主动轮询不可用回退到 CDP 响应捕获

    async def goto_live(self):
        """建 CDP session(精简+学习共用) + flv 精确路由,然后导航到 liveBuild 页。

        rev13 核心改进: 不再常驻监听每个响应(rev11/rev12 的 RSS 膨胀根因)。
        改为:
          - 仅短暂监听 Network.requestWillBeSent,动态学到 get_live_info/check_live_status
            的完整请求规格(url+method+headers+body),两个都学到后立即移除监听;
          - 之后由 poll() 主动 page.evaluate(fetch) 同源重放拉取(见 poll 文档)。
        CDP session 仍保留给 setBlockedURLs 精简使用。
        """
        # rev13: 建 CDP session,启用 Network 域,注册【一次性】请求学习监听
        try:
            self._cdp = await self.page.context.new_cdp_session(self.page)
            await self._cdp.send("Network.enable")
            self._cdp_ready = True
            # 仅短暂监听请求以【动态学到】直播检测 API(学到后由 _on_req_learn 自动关闭)
            self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
            self._learn_deadline = time.time() + 15  # 15s 内学不到则回退 CDP
            logger.debug(f"[live:{self.account_id}] CDP session 已建(Network 域已启用,启动 API 学习)")
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] CDP session 创建失败(降级:主动轮询不可用,将无直播数据): {e}")
        await self._install_flv_route()
        try:
            await self.page.goto(LIVE_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] goto liveBuild 失败: {e}")

    async def _install_flv_route(self):
        """page.route 精确拦截 flv/m3u8: 捕获 stream_url 后 abort 中止下载。

        仅 2 条精确路由(**/*.flv* / **/*.m3u8*),非 **/* 海量路由,无 Playwright 代理对象累积泄漏。
        这是原代码能正常出视频的方案,经实测验证最稳。
        """
        try:
            await self.page.route("**/*.flv*", self._handle_flv)
            await self.page.route("**/*.m3u8*", self._handle_flv)
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] flv route 安装失败(降级: 靠轮询兜底): {e}")

    async def _handle_flv(self, route):
        """flv/m3u8 请求: 先抓 URL 作为 stream_url,再 abort 中止下载(后端不缓存流体,
        前端 flv.js 独立拉流)。"""
        u = route.request.url or ""
        if ".flv" in u or ".m3u8" in u or "wxlivecdn" in u or "trtc" in u:
            if u != self.stream_url:
                self.stream_url = u
                logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(来自 route): {u[:80]}...")
            # 拿到 stream_url: 触发 stage2(在 route handler 里用 create_task 避免阻塞 abort)
            if self._slim_stage < 2:
                self._slim_task = asyncio.create_task(self._apply_slimming(2))
        try:
            await route.abort()
        except Exception:
            pass

    # ======================== rev10: 阶段化内存精简 ========================

    async def _apply_slimming(self, stage: int):
        """拿到直播信号后,在浏览器层(CDP)阻断非必要资源(0 Python 对象)。

        必须在 SPA 初始化完成后调用(rev6~rev8 教训: goto 前阻断会杀初始化)。
        stage1: 拦图片+媒体 -> 弹幕头像等图片响应不再产生 Python Response 对象。
        stage2: 追加拦 FLV + 解除 page.route FLV 路由 + JS 停 <video> ->
                FLV 重试在浏览器层被拦,不再产生 Python Route 对象(主泄漏源)。

        rev13: 复用 goto_live 建的统一 CDP session(self._cdp),不再新建。
        """
        if self._slim_stage >= stage or not self.page:
            return
        self._slim_stage = stage  # 先置位防重入(asyncio 单线程,await 前设置即可)
        try:
            cdp = self._cdp
            if cdp is None:
                # CDP session 在 goto_live 时建;若失败则尝试重建(极端情况)
                cdp = await self.page.context.new_cdp_session(self.page)
                await cdp.send("Network.enable")
                self._cdp = cdp
                self._cdp_ready = True
            urls = list(_SLIM_MEDIA_URLS)
            if stage >= 2:
                urls += _SLIM_FLV_URLS
            await cdp.send("Network.setBlockedURLs", {"urls": urls})
            logger.info(f"[live:{self.account_id}] 内存精简 stage{stage} 生效(浏览器层阻断 {len(urls)} 类资源)")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 内存精简 stage{stage} 失败(不影响抓取): {e}")
            return
        if stage >= 2:
            # FLV 已有 CDP 兜底拦截,解除 page.route(每次播放器重试都会创建 Route 对象)
            for pat in ("**/*.flv*", "**/*.m3u8*"):
                try:
                    await self.page.unroute(pat)
                except Exception:
                    pass
            # 停掉页面预览播放器,源头减少 FLV 重试
            try:
                await self.page.evaluate("""()=>{
                    document.querySelectorAll('video').forEach(v=>{
                        try{v.pause();v.removeAttribute('src');v.load();}catch(e){}
                    });
                }""")
            except Exception:
                pass

    # ======================== rev13: 主动轮询(替代常驻响应监听) ========================

    def _on_req_learn(self, event):
        """rev13: 一次性学习 get_live_info/check_live_status 请求规格(url+method+headers+body)。

        学到两个即移除本监听,之后稳态直播期间不再有任何常驻网络事件进 Python。
        """
        try:
            req = (event.get("request") or {})
            url = req.get("url", "")
            if "get_live_info" in url:
                self._learned["live_info"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_info API: {url[:90]}")
            elif "check_live_status" in url:
                self._learned["live_status"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_status API: {url[:90]}")
            if "live_info" in self._learned and "live_status" in self._learned:
                # 两个都学到 -> 关闭常驻监听,消除事件 churn
                try:
                    self._cdp.remove_listener("Network.requestWillBeSent", self._on_req_learn)
                except Exception:
                    pass
                self._learning = False
                logger.info(f"[live:{self.account_id}] 已动态学到直播检测 API,关闭常驻请求监听(改为主动轮询)")
        except Exception:
            pass  # CDP 回调不允许异常上抛

    def _capture_req(self, req):
        """提取请求规格(供 page.evaluate(fetch) 同源重放)。"""
        return {
            "url": req.get("url", ""),
            "method": req.get("method", "POST"),
            "headers": req.get("headers", {}) or {},
            "postData": req.get("postData"),
        }

    async def poll(self):
        """rev13: 主动轮询 get_live_info/check_live_status(同源 fetch,替代常驻 CDP 响应监听)。

        机制:
          - 学习未完成:等待页面自身发出的请求被 _on_req_learn 学到;若超过 _learn_deadline
            仍未学到(如接口未触发),自动回退 CDP 响应捕获(保证有数据)。
          - 已学到:用学到的参数经 page.evaluate(fetch) 同源重放(credentials:include 自动带
            cookie)拉取最新数据,Python 侧仅产生一次 fetch 往返的少量对象。
          - 主动重放连续 3 次失败(接口要求一次性 token 等):回退 CDP 响应捕获(功能不退化)。
        """
        # 学习未完成:等学到;超时则回退 CDP
        if self._learning and not self._cdp_fallback:
            if time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
        # 已回退 CDP: 数据由 responseReceived 被动填充,这里仅返回快照
        if self._cdp_fallback:
            return await self.snapshot()
        # 主动重放两个 API
        try:
            spec_info = self._learned.get("live_info")
            if spec_info:
                txt = await self._fetch_replay(spec_info)
                if txt:
                    await self._parse_live_info(txt)
            spec_status = self._learned.get("live_status")
            if spec_status:
                txt2 = await self._fetch_replay(spec_status)
                if txt2:
                    await self._parse_live_status(txt2)
            self._poll_fail_streak = 0
        except Exception as e:
            self._poll_fail_streak += 1
            logger.debug(f"[live:{self.account_id}] 主动轮询失败({self._poll_fail_streak}/3): {e}")
            if self._poll_fail_streak >= 3:
                await self._enable_cdp_fallback()
        return await self.snapshot()

    async def _fetch_replay(self, spec):
        """同源重放学到的请求(fetch,credentials:include 自动带 cookie)。返回响应体文本。"""
        js = """async (spec) => {
            const headers = Object.assign({'Content-Type':'application/json'}, spec.headers || {});
            const init = {
                method: spec.method || 'POST',
                credentials: 'include',
                headers: headers,
                body: spec.postData || null,
            };
            const r = await fetch(spec.url, init);
            return await r.text();
        }"""
        try:
            return await self.page.evaluate(js, spec)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] fetch 重放失败: {e}")
            return ""

    async def _enable_cdp_fallback(self):
        """主动轮询失败/学不到 URL 时回退: 重臂 rev11 的 CDP 响应捕获(功能优先,内存次之)。"""
        if self._cdp_fallback:
            return
        try:
            if self._cdp is not None:
                self._cdp.on("Network.responseReceived", self._on_cdp_response)
                self._cdp_fallback = True
                logger.warning(f"[live:{self.account_id}] 直播检测回退到 CDP 响应捕获(主动轮询不可用)")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 启用 CDP 回退失败: {e}")

    # ---- rev13: 解析逻辑(主动轮询 + CDP 回退共用) ----
    async def _parse_live_info(self, txt):
        """get_live_info 响应体 -> liveStats + 兜底 liveObjectId -> stage1。返回是否解析到数据。"""
        if not txt:
            return False
        try:
            data = json.loads(txt)
        except Exception:
            return False
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
        # 拿到 liveStats/liveObjectId 说明 SPA 已初始化完成 -> stage1(拦图片/媒体)
        if stats or self.live_object_id:
            await self._apply_slimming(1)
        return True

    async def _parse_live_status(self, txt):
        """check_live_status 响应体 -> liveObjectId + stream_url(.flv 流) -> stage1/2。

        这是 stream_url 的【主要可靠来源】(实测确认 audiencePlayUrl 为 // 开头的 .flv URL)。
        """
        if not txt:
            return
        try:
            data = json.loads(txt)
        except Exception:
            return
        d = data.get("data") or {}
        live_id = d.get("liveObjectId") or self._find_live_object_id(d)
        if live_id:
            if not self.live_object_id:
                logger.info(f"[live:{self.account_id}] 拿到 liveObjectId={live_id}(来自 check_live_status),dashboard 抓取启用")
            self.live_object_id = str(live_id)
            # 拿到 liveObjectId 说明 SPA 已初始化 -> stage1(拦图片/媒体)
            await self._apply_slimming(1)
        # .flv 流 URL: 优先 audiencePlayUrl,兜底 liveStreamUrlInfo.liveCdnUrl(均 // 开头 -> 补 https:)
        flv = d.get("audiencePlayUrl") or ((d.get("liveStreamUrlInfo") or {}).get("liveCdnUrl") or "")
        if flv and (".flv" in flv or ".m3u8" in flv):
            url = flv if flv.startswith("http") else "https:" + flv
            if url != self.stream_url:
                self.stream_url = url
                logger.info(f"[live:{self.account_id}] 拿到 .flv 流 URL(来自 check_live_status): {url[:80]}...")
            # 拿到 stream_url -> stage2(追加拦 FLV + 解除 page.route + 停 video)
            await self._apply_slimming(2)
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
                # 同样触发 stage2
                await self._apply_slimming(2)

    # ======================== rev11 回退: CDP 响应捕获(仅回退模式启用) ========================

    def _on_cdp_response(self, event):
        """CDP Network.responseReceived 回调(轻量 dict,无 Python Response 对象)。

        仅作 rev13 主动轮询失败时的【回退】捕获: 对 get_live_info / check_live_status
        两个目标 URL 调用 getResponseBody 读体,其余数千请求立即返回。用 create_task 避免
        阻塞 CDP 事件分发线程。
        """
        try:
            url = (event.get("response") or {}).get("url", "")
            if "channels.weixin.qq.com" not in url:
                return
            if "get_live_info" in url:
                asyncio.create_task(self._capture_cdp(event.get("requestId", "")))
            elif "check_live_status" in url:
                asyncio.create_task(self._capture_live_status_cdp(event.get("requestId", "")))
        except Exception:
            pass  # CDP 回调不允许异常上抛

    async def _read_cdp_body(self, request_id: str) -> str:
        """通过 CDP Network.getResponseBody 读取指定请求的响应体文本(回退模式用)。"""
        try:
            if self._cdp is None:
                return ""
            result = await self._cdp.send("Network.getResponseBody", {"requestId": request_id})
            return result.get("body", "") or ""
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] CDP getResponseBody 失败: {e}")
            return ""

    async def _capture_cdp(self, request_id: str):
        """get_live_info 响应(CDP 回退版): 读体 -> 解析。"""
        try:
            txt = await self._read_cdp_body(request_id)
            await self._parse_live_info(txt)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] get_live_info 解析失败: {e}")

    async def _capture_live_status_cdp(self, request_id: str):
        """check_live_status 响应(CDP 回退版): 读体 -> 解析。"""
        try:
            txt = await self._read_cdp_body(request_id)
            await self._parse_live_status(txt)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] check_live_status 解析失败: {e}")

    # ======================== 关闭清理 ========================

    async def close(self):
        """关闭前清理: 移除所有 CDP 监听 + detach session + 解除 flv 路由 + 断 page 引用。

        rev13: 可能注册过 requestWillBeSent(学习) 与 responseReceived(回退)两种监听,均移除。
        """
        # 移除 CDP 监听(学习 + 回退)
        if self._cdp is not None:
            try:
                self._cdp.remove_listener("Network.requestWillBeSent", self._on_req_learn)
            except Exception:
                pass
            try:
                self._cdp.remove_listener("Network.responseReceived", self._on_cdp_response)
            except Exception:
                pass
        # 解除 FLV 路由
        for pat in ("**/*.flv*", "**/*.m3u8*"):
            try:
                await self.page.unroute(pat)
            except Exception:
                pass
        # detach CDP session(精简 + 学习 + 回退共用)
        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
            self._cdp_ready = False
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

    async def snapshot(self):
        """返回当前抓取结果(无告警,供检测轮询调用)。"""
        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
            "metrics": self._dashboard_cache or {},
        }

    async def fetch(self):
        """主动轮询一次并返回结果;附带 stream_url 诊断(供日志排查前端无视频时定位)。"""
        await self.poll()
        if not self.stream_url and not self._flv_req_warned:
            self._flv_req_warned = True
            logger.warning(f"[live:{self.account_id}] stream_url 仍为空(live_object_id={self.live_object_id},"
                           f"检查 check_live_status 是否返回 audiencePlayUrl/liveCdnUrl)")
        return await self.snapshot()
