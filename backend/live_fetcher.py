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
import os
import re
import time
import logging
from collections import deque
from datetime import datetime
from .selectors import LIVE_URL
from .metrics import extract_all, shrink_conv
from . import wx_http

# ======================== rev43: 已学 API 规格持久化 ========================
# 现象:学到的 get_live_info/check_live_status 规格只活在内存。live_page 一重建
# (定期释放 renderer / 省内存关浏览器后重开)就得从零重学,而学习依赖页面自发请求,
# 时机过了就学不到 -> 误判"未在直播"。落到磁盘后重建即继承,首轮 poll 就能出数。
_SPEC_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "live_api_spec.json")


def _load_spec_cache(account_id):
    """读该账号已学到的 API 规格(文件不存在/损坏一律返回 {})。"""
    try:
        with io_open(_SPEC_CACHE_PATH) as f:
            data = json.load(f)
        spec = data.get(account_id) or {}
        # 只认结构完整的条目,防止半截文件把轮询带进死循环
        return {k: v for k, v in spec.items()
                if isinstance(v, dict) and v.get("url")}
    except Exception:
        return {}


def _save_spec_cache(account_id, learned):
    """写回该账号的 API 规格(tmp -> os.replace 原子写;失败静默,不影响抓取)。"""
    try:
        os.makedirs(os.path.dirname(_SPEC_CACHE_PATH), exist_ok=True)
        try:
            with io_open(_SPEC_CACHE_PATH) as f:
                data = json.load(f)
        except Exception:
            data = {}
        clean = {}
        for k, v in (learned or {}).items():
            if not isinstance(v, dict) or not v.get("url"):
                continue
            headers = {hk: hv for hk, hv in (v.get("headers") or {}).items()
                       if hk.lower() != "cookie"}   # cookie 绝不落盘
            clean[k] = {"url": v["url"], "method": v.get("method", "POST"),
                        "headers": headers, "postData": v.get("postData")}
        if not clean:
            return
        data[account_id] = clean
        tmp = _SPEC_CACHE_PATH + ".tmp"
        with io_open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _SPEC_CACHE_PATH)
    except Exception:
        pass


def io_open(path, mode="r"):
    """小工具:统一 utf-8 打开(读写都走这里,避免各处编码不一致)。"""
    return open(path, mode, encoding="utf-8")

logger = logging.getLogger("sphgj")

# dashboardV4 数据 API(同源 POST,在 liveBuild page fetch)
DASHBOARD_DATA_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/statistic/get_ec_conversion_dashboard_data_v3"
DISTRIBUTION_CHANNEL_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/svrkit/MmFinderECAssistantDataSvr/getLiveDistributionChannel"
EC_DATA_SUMMARY_API = "https://channels.weixin.qq.com/micro/statistic/cgi-bin/mmfinderassistant-bin/statistic/get_live_ec_data_summary"
DASHBOARD_PAGE_URL = "https://channels.weixin.qq.com/micro/statistic/dashboardV4"
DASHBOARD_PAGE_URL_ENC = "https%3A%2F%2Fchannels.weixin.qq.com%2Fmicro%2Fstatistic%2FdashboardV4"

# 渠道大类:newLiveDstChannelType 1=公域 / 2=加热 / 4=私域
PUBLIC_CHANNEL_TYPE = 1
DASHBOARD_INTERVAL = 60  # dashboard 数据抓取间隔(秒);实际由 account_manager 读 config.dashboard_interval_sec 控制

# ======================== rev21: 超时闸 + 并发闸 ========================
# 取证背景(2026-09-03,见 .workbuddy/mem/):进程长跑 49h 后 RSS 1670MB,
# 其中 tracked 891MB(349 万个 dict,全是 dashboard conv 响应的序列骨架)、
# untracked 779MB;同时有 7343 个 asyncio Task 存活(每个 Task 自带一个
# contextvars.Context,故 Context/hamt 计数同步走高)。
#
# 两大成因:
#  1) dashboard 每轮抓取的 conv 响应骨架被某个外部容器永久持有,一轮留 ~476 个 dict;
#  2) CDP 回退模式下 _on_cdp_response 对每个响应 asyncio.create_task 一个
#     fire-and-forget 任务 —— 无超时、无并发上限、无追踪。Playwright 的
#     cdp.send / page.evaluate / page.unroute 在会话异常时【可能永不返回】,
#     挂住的任务其协程帧会永久钉住局部变量(含响应体文本),且 str 不被 gc 跟踪
#     -> 这就是 untracked 内存的主要来源。
#
# 因此:所有可能悬挂的 await 一律套超时;fire-and-forget 任务一律有并发上限 +
# 引用追踪 + 可取消。
_EVAL_TIMEOUT = 20.0       # page.evaluate / page.unroute 单次超时(秒)
_CDP_OP_TIMEOUT = 10.0     # 单次 CDP send 超时(秒)
_CDP_BODY_TIMEOUT = 6.0    # Network.getResponseBody 超时(秒)
_CDP_MAX_INFLIGHT = 4      # 每个 LiveFetcher 同时在飞的 CDP 回退任务上限
_CDP_SEEN_MAX = 256        # requestId 去重窗口(有界,不能无限增长)


async def _with_timeout(coro, timeout):
    """await 一个协程并强制超时。超时/异常一律返回 None,绝不把异常抛给调用方。

    超时会【取消】协程 —— 这一点是关键:被取消的协程其帧会被销毁,局部变量
    (可能钉着数 MB 的响应文本)随之释放;不加超时则会永久悬挂并泄漏。
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception:
        # 超时(TimeoutError)与 CDP/Playwright 异常一视同仁:本轮放弃,下轮重试
        return None

# ======================== rev10: 阶段化内存精简 ========================
# stage1(拿到 liveStats/liveObjectId): 浏览器层拦图片+媒体(不含 css/js/api)
# stage2(拿到 stream_url): 追加拦 FLV + 解除 page.route FLV 路由 + 停 video
_SLIM_MEDIA_URLS = [
    "*.png*", "*.jpg*", "*.jpeg*", "*.gif*", "*.svg*", "*.webp*", "*.ico*", "*.bmp*",
    "*.mp4*", "*.webm*", "*.mp3*", "*.wav*", "*.ogg*", "*.m4a*",
]
_SLIM_FLV_URLS = ["*.flv*", "*.m3u8*", "*wxlivecdn*", "*trtc*"]


# ======================== rev47: 全项目唯一的直播判据 ========================
# 为什么要统一:rev43~46 期间"判定口径不一致"造成了三类连环误判 ——
#   ① _wait_for_live_detection 拿 live_object_id 就提前返回,与判定(只认 live_stats)不一致
#   ② _open_and_detect_live 把 stream_url 当在播信号,而 _live_loop 只认 live_stats
#      -> 检测说"在播"、循环说"信号中断",白跑 3 次重试 + reload + 12s
#   ③ 两处各自维护 fresh 窗口(30s / 90s),改一处忘另一处
# 从 rev47 起:任何地方判"是否在播"都必须调 judge_live(),不要再就地写表达式。

# DOM 关键词(保守集合)。2026-10-28 三个账号 check_live_status 完整响应 dump 确认:
# 未开播时只有 status=0,没有 isLive/isLiving 这类布尔位,也没有播放地址;
# 所以页面文本是最廉价、最不依赖接口字段的兜底判据。
# 词表刻意保守:"拿不准一律 None",绝不用它下"未播"的结论(只作弱正向)。
_DOM_NOT_LIVE_WORDS = ("未开播", "暂未开播", "当前未开播", "直播已结束", "已结束直播", "暂无直播")
_DOM_LIVE_WORDS = ("正在直播", "直播中", "结束直播", "直播时长", "在线人数")


def read_dom_state_from_text(txt):
    """从直播大屏页文本推断状态。返回 (True=看起来在播 / False=看起来未播 / None=判断不了, 命中词)。"""
    if not txt:
        return None, ""
    for w in _DOM_NOT_LIVE_WORDS:
        if w in txt:
            return False, w
    for w in _DOM_LIVE_WORDS:
        if w in txt:
            return True, w
    return None, ""


def judge_live(live_stats=None, stats_fresh=True, stream_url=None,
               allow_stream_url=True, dom_state=None, dom_fresh=True,
               status=None, log_account=""):
    """统一直播判据。返回 (is_live: bool, reason: str)。

    优先级(按实测可靠度从高到低):
      1. live_stats 新鲜        —— 最强,get_live_info / check_live_status 带 liveStats 才有
      2. DOM 明确"在播"          —— rev47 兜底,不依赖接口字段
      3. DOM 明确"未开播"        —— 同上,负向
      4. stream_url(.flv/.m3u8) —— 检测阶段可用;长驻页面不要用,它是 sticky 的
                                   (拿到一次就再也不会被清空,用它判活会永远判在播)
      5. status == 0            —— check_live_status.status,0=未开播(三账号 dump 实证)

    allow_stream_url=False 供 _live_loop 这类"长驻页面判信号是否还活着"的场景使用。
    """
    if live_stats and stats_fresh:
        return True, "live_stats"
    if dom_state is True and dom_fresh:
        return True, "dom:在播"
    if dom_state is False and dom_fresh:
        return False, "dom:未开播"
    if allow_stream_url and stream_url:
        return True, "stream_url"
    if status == 0:
        return False, "status=0"
    return False, "no_signal"


class LiveFetcher:
    def __init__(self, page, account_id, learned=None, account=None):
        """learned: rev16 F4 —— 继承上一实例的 API 规格 {name: spec}。

        重建 live_page(信号丢失自愈 / 定期释放 renderer)时传入旧实例的 _learned,
        新实例可跳过 requestWillBeSent 学习流程直接 poll,消除重学期间的数秒恢复空窗。
        继承来的 spec 仍可能因会话失效而重放失败,因此 poll 的"连续失败回退 CDP"兜底不变。
        """
        self.page = page
        self.account_id = account_id
        # rev50: 账号 dict（含 _aid / _log_finder_id / storage_state）。给了就让 dashboard
        # 走纯 HTTP，不必等页面渲染；不给则只能读 localStorage（浏览器路径）。
        self._account = account or {}
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
        # rev16 F4: learned 非空时视为"已学会",跳过 requestWillBeSent 监听直接 poll
        self._learned = dict(learned) if learned else {}   # {"live_info": spec, "live_status": spec}
        # rev43: 内存没继承到 -> 读磁盘缓存(data/live_api_spec.json),避免每次重建都从零学
        if not self._learned:
            cached = _load_spec_cache(account_id)
            if cached:
                self._learned = cached
                # rev51: 每次 reconstruct 都会走这里(轻量探测每 120s × N 账号),
                # 原来打 INFO 会淹掉真正有用的探测结果(实测 600 行日志里 109 条是这个)。
                logger.debug(f"[live:{account_id}] 从磁盘缓存载入已学 API 规格: {sorted(cached)}")
        self._inherited = bool(self._learned)              # 规格是否来自继承/缓存(用于日志区分)
        self._learning = not self._learned                 # 已继承则无需再学
        self._learn_deadline = 0.0      # 学习超时阈值(>此时间未学到则回退 CDP)
        self._poll_fail_streak = 0      # 主动轮询连续失败计数
        self._cdp_fallback = False      # 主动轮询不可用回退到 CDP 响应捕获
        self._replay_tick = 0           # rev43: 回退 CDP 后的主动重放节流计数
        self._reload_nudged = False     # rev43: 学习超时只自动 reload 一次,防抖
        self._urls_seen = []            # rev44: 学习窗口内采样到的 live 相关请求 URL(限 5 条)
        self._status_body_dumped = False  # rev45: check_live_status 原始响应只 dump 一次
        self.live_flag = None           # rev46: check_live_status 里的布尔直播标志(True/False/未知)
        # rev47: 统一判据所需的两个事实来源
        self.status_num = None          # check_live_status.status(0=未开播,实测确认)
        self.dom_state = None           # 直播大屏页文本推断(True/False/None)
        self.dom_ts = 0.0               # dom_state 的采样时间
        self._dom_dumped = False        # 页面文本只落盘一次(诊断用)
        # rev57: 看板字段缺失定位用
        self.live_start_ts = None       # check_live_status.createTime(开播时刻,Unix 秒)
        self._empty_info_warn_ts = 0.0  # get_live_info 返回空 data 的告警限流(秒)
        # rev57b: 「场次 ID 已刷新」日志的幂等标记。缓存模板里的旧 ID 是固定的,
        # 不去重就会每轮重放都打一条 INFO(4 账号约 1800 条/小时),把 run.log 淹掉。
        self._oid_logged = None         # 上次已打日志的 (旧ID, 新ID) 组合
        # rev21: CDP 回退任务的并发闸与追踪(防 fire-and-forget 任务无限堆积)
        self._cdp_tasks = set()         # 在飞的回退任务引用(完成自动 discard,close 时全部 cancel)
        self._cdp_seen = deque(maxlen=_CDP_SEEN_MAX)   # 最近处理过的 requestId(有界去重)
        self._cdp_drop_warned = False   # 并发打满时只 warn 一次,避免日志刷屏

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
            if self._learning:
                # 仅短暂监听请求以【动态学到】直播检测 API(学到后由 _on_req_learn 自动关闭)
                self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
                self._learn_deadline = time.time() + 15  # 15s 内学不到则回退 CDP
                logger.debug(f"[live:{self.account_id}] CDP session 已建(Network 域已启用,启动 API 学习)")
            elif "live_info" not in self._learned:
                # rev44: 继承到 live_status 却缺 live_info —— 这样拿不到 liveStats,
                # 而 liveStats 是 is_live 的唯一硬判据,等于永远判"未在直播"。
                # 重开一个 15s 学习窗口专补 live_info(poll 照常用已有规格主动轮询,不等它)。
                self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
                self._learning = True
                self._learn_deadline = time.time() + 15
                logger.info(f"[live:{self.account_id}] 继承规格缺 live_info,"
                            f"重开 15s 学习窗口补学(已有: {sorted(self._learned)})")
            else:
                # rev16 F4: 继承到规格,不注册学习监听 —— 零事件 churn,首轮 poll 即可出数
                logger.info(f"[live:{self.account_id}] 继承已学 API 规格(跳过学习监听),"
                            f"可直接主动轮询: {sorted(self._learned.keys())}")
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
                # rev21: 任务完成后立刻解绑引用 —— 已完成的 Task 仍持有协程帧与其局部变量
                _t = asyncio.create_task(self._apply_slimming(2))
                _t.add_done_callback(self._on_slim_task_done)
                self._slim_task = _t
        try:
            await route.abort()
        except Exception:
            pass

    # ======================== rev10: 阶段化内存精简 ========================

    def _on_slim_task_done(self, t):
        """rev21: stage2 精简任务结束后释放引用(完成的 Task 仍持有协程帧+局部变量)。"""
        try:
            if not t.cancelled():
                t.exception()   # 取走异常,避免 "exception never retrieved" 常驻
        except Exception:
            pass
        if self._slim_task is t:   # 只清自己,别误清新任务
            self._slim_task = None

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
                # rev21: 加超时 —— new_cdp_session 在浏览器无响应时会永久挂起
                cdp = await _with_timeout(
                    self.page.context.new_cdp_session(self.page), _CDP_OP_TIMEOUT)
                if cdp is None:
                    return
                await _with_timeout(cdp.send("Network.enable"), _CDP_OP_TIMEOUT)
                self._cdp = cdp
                self._cdp_ready = True
            urls = list(_SLIM_MEDIA_URLS)
            if stage >= 2:
                urls += _SLIM_FLV_URLS
            # rev21: 超时闸 —— cdp.send 永不返回会把调用方(可能是 fire-and-forget 任务)永久挂住
            if await _with_timeout(
                    cdp.send("Network.setBlockedURLs", {"urls": urls}), _CDP_OP_TIMEOUT) is None:
                return
            logger.info(f"[live:{self.account_id}] 内存精简 stage{stage} 生效(浏览器层阻断 {len(urls)} 类资源)")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 内存精简 stage{stage} 失败(不影响抓取): {e}")
            return
        if stage >= 2:
            # FLV 已有 CDP 兜底拦截,解除 page.route(每次播放器重试都会创建 Route 对象)
            for pat in ("**/*.flv*", "**/*.m3u8*"):
                # rev21: unroute 同样可能挂起(页面正导航时)
                await _with_timeout(self.page.unroute(pat), _CDP_OP_TIMEOUT)
            # 停掉页面预览播放器,源头减少 FLV 重试
            await _with_timeout(self.page.evaluate("""()=>{
                    document.querySelectorAll('video').forEach(v=>{
                        try{v.pause();v.removeAttribute('src');v.load();}catch(e){}
                    });
                }"""), _EVAL_TIMEOUT)

    # ======================== rev13: 主动轮询(替代常驻响应监听) ========================

    def _on_req_learn(self, event):
        """rev13: 一次性学习 get_live_info/check_live_status 请求规格(url+method+headers+body)。

        学到两个即移除本监听,之后稳态直播期间不再有任何常驻网络事件进 Python。
        """
        try:
            req = (event.get("request") or {})
            url = req.get("url", "")
            # rev45: 只采样 cgi-bin 的 API 请求(静态 js/页面 URL 不是接口),限 5 条
            if "live_info" not in self._learned and len(self._urls_seen) < 5 \
                    and "cgi-bin" in url and "live" in url.lower() \
                    and url not in self._urls_seen:
                self._urls_seen.append(url)
                logger.info(f"[live:{self.account_id}] live API 采样"
                            f"({len(self._urls_seen)}/5): {url[:150]}")
            learned_before = len(self._learned)
            if "get_live_info" in url:
                self._learned["live_info"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_info API: {url[:90]}")
            elif "check_live_status" in url:
                self._learned["live_status"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_status API: {url[:90]}")
            if len(self._learned) > learned_before:
                # rev43: 学到【一个】就立即剔除 cookie 并落盘。原实现要等两个都学到才处理,
                # 于是"只学到 live_status"的实例既用不上也不留档,下次重建还得重学。
                self._strip_cookie_headers()
                _save_spec_cache(self.account_id, self._learned)
            if "live_info" in self._learned and "live_status" in self._learned:
                # 两个都学到 -> 关闭常驻监听,消除事件 churn
                try:
                    self._cdp.remove_listener("Network.requestWillBeSent", self._on_req_learn)
                except Exception:
                    pass
                self._learning = False
                # rev15 P3: 学完即剔除 cookie,释放常驻内存
                self._strip_cookie_headers()
                logger.info(f"[live:{self.account_id}] 已动态学到直播检测 API,关闭常驻请求监听(改为主动轮询)")
        except Exception:
            pass  # CDP 回调不允许异常上抛

    def _capture_req(self, req):
        """提取请求规格(供 page.evaluate(fetch) 同源重放)。

        rev57: postData 里的 liveObjectId 一律清空后再入库。它是"场次变量"不是"接口常量",
        冻结到 data/live_api_spec.json 就会在换场/换天后一直拿旧场次去请求,接口返回
        errCode=0 但 data 为空 -> liveStats 静默断供(2026-10-29 实测:5 账号 4 个不匹配,
        唯一匹配的那台看着正常)。重放时由 _fresh_post_data 填当前值。
        """
        post = req.get("postData")
        if post and "liveObjectId" in post:
            try:
                o = json.loads(post)
                if isinstance(o, dict) and "liveObjectId" in o:
                    o["liveObjectId"] = ""
                    post = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
        return {
            "url": req.get("url", ""),
            "method": req.get("method", "POST"),
            "headers": req.get("headers", {}) or {},
            "postData": post,
        }

    def _strip_cookie_headers(self):
        """rev15 P3: 学到 API 规格后剔除 cookie,释放常驻内存。

        重放走 page.evaluate(fetch) 且带 credentials:'include',浏览器会【自动附加】当前源
        的 cookie —— 显式传递 cookie 既冗余,又是 headers 里体积最大的一项(视频号会话
        cookie 常 2~8KB,且随会话增长)。4 账号 × 2 接口常驻,累积可达数十 KB 且永不释放。

        仅剔除 cookie,保留其余功能性 header(如 authorization/x-wechat-* 等),
        避免误伤重放鉴权导致回退到 CDP 高内存模式。
        """
        for spec in self._learned.values():
            headers = spec.get("headers") or {}
            for k in ("cookie", "Cookie"):
                headers.pop(k, None)

    async def poll(self):
        """rev13: 主动轮询 get_live_info/check_live_status(同源 fetch,替代常驻 CDP 响应监听)。

        机制:
          - 学习未完成:等待页面自身发出的请求被 _on_req_learn 学到;若超过 _learn_deadline
            仍未学到(如接口未触发),自动回退 CDP 响应捕获(保证有数据)。
          - 已学到:用学到的参数经 page.evaluate(fetch) 同源重放(credentials:include 自动带
            cookie)拉取最新数据,Python 侧仅产生一次 fetch 往返的少量对象。
          - 主动重放连续 3 次失败(接口要求一次性 token 等):回退 CDP 响应捕获(功能不退化)。
        """
        # rev43: 只要学到【任一】接口就主动重放,不再要求两个都学到。
        # 原逻辑 _learning 是"全有或全无"开关:15s 内只学到 live_status(缺 live_info)
        # 就完全不主动轮询,直接掉进 CDP 被动捕获 —— 而此刻页面早已把 check_live_status
        # 发完,被动监听永远等不到新响应 -> 误判"未在直播"(2026-10-28 2 号事故)。
        if not self._learned:
            # 一个都没学到:等学习窗口;超时则回退 CDP(回退时自动 reload 重触发请求)
            if self._learning and not self._cdp_fallback and time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
        # rev44: 已有一份规格但学习窗口到期 -> 摘监听,别留常驻事件 churn
        if self._learning and time.time() > self._learn_deadline:
            self._end_learning("学习窗口结束")
        # 已回退 CDP:以被动捕获为主,但仍每 3 轮主动重放一次
        # (直播间不刷新时,页面可能长时间不再发 check_live_status,纯被动会饿死)
        if self._cdp_fallback:
            self._replay_tick = (self._replay_tick + 1) % 3
            if self._replay_tick != 0:
                return await self.snapshot()
        # 主动重放两个 API
        # rev57: 顺序必须是 status -> info。live_status 是本场 liveObjectId 的唯一来源,
        # info 的重放 body 要用它来重写(见 _fresh_post_data)。原来 info 在前,每一轮
        # 都拿上一轮/上次进程的旧场次 ID 去打 get_live_info -> 返回空 data -> liveStats 全空。
        try:
            spec_status = self._learned.get("live_status")
            if spec_status:
                txt2 = await self._fetch_replay(spec_status)
                if txt2:
                    await self._parse_live_status(txt2)
            spec_info = self._learned.get("live_info")
            if spec_info:
                txt = await self._fetch_replay(spec_info)
                if txt:
                    await self._parse_live_info(txt)
            self._poll_fail_streak = 0
        except Exception as e:
            self._poll_fail_streak += 1
            logger.debug(f"[live:{self.account_id}] 主动轮询失败({self._poll_fail_streak}/3): {e}")
            if self._poll_fail_streak >= 3:
                await self._enable_cdp_fallback()
        return await self.snapshot()

    def _end_learning(self, reason: str):
        """rev44: 结束学习窗口(摘监听),不影响已有规格的主动轮询。"""
        self._learning = False
        try:
            if self._cdp is not None:
                self._cdp.remove_listener("Network.requestWillBeSent", self._on_req_learn)
        except Exception:
            pass
        logger.info(f"[live:{self.account_id}] {reason},未学到 live_info;"
                    f"继续按已有规格轮询: {sorted(self._learned)}")

    def _schedule_reload(self, reason: str):
        """rev43: 后台 reload 一次直播页,逼 SPA 重新发出 check_live_status/get_live_info。

        只做一次(同一实例不重复),任务进 _cdp_tasks 追踪,close() 时会被 cancel。
        这是 CDP 回退能起作用的【前提】:被动捕获看不到已经发生过的请求。
        """
        if self._reload_nudged or self.page is None:
            return
        self._reload_nudged = True

        async def _do():
            try:
                if self.page is None:
                    return
                await _with_timeout(self.page.reload(wait_until="domcontentloaded"), 25)
                logger.info(f"[live:{self.account_id}] 已重载直播页({reason}),等页面重新发出检测请求")
            except Exception as e:
                logger.debug(f"[live:{self.account_id}] 重载直播页失败: {e}")

        try:
            t = asyncio.create_task(_do())
            self._cdp_tasks.add(t)
            t.add_done_callback(self._cdp_tasks.discard)
        except Exception:
            pass

    async def relearn(self):
        """rev43: 检测阶段第二轮用 —— 重载直播页,并(在仍无规格时)重开学习窗口。

        返回是否真的重载了(页面已关/重载失败返回 False)。
        """
        if self.page is None:
            return False
        self.updated_at_ts = 0.0
        r = await _with_timeout(self.page.reload(wait_until="domcontentloaded"), 25)
        if r is None and getattr(self.page, "is_closed", lambda: False)():
            return False
        if not self._learned and self._cdp is not None:
            # 仍无规格:重新 arm 学习监听(10s 窗口),页面重载后请求会重新发出
            try:
                self._learning = True
                self._learn_deadline = time.time() + 10
                self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
                logger.info(f"[live:{self.account_id}] 重载后重新开始学习直播检测 API(10s 窗口)")
            except Exception:
                pass
        # 已回退 CDP 的实例:reload 后被动捕获自然重新工作,清一下节流计数
        self._replay_tick = 0
        return True

    def _fresh_post_data(self, spec):
        """rev57: 重放前用【当前场次 ID】+【当前时间戳】重写缓存的请求体。

        为什么必须重写:data/live_api_spec.json 缓存的是"学到那一刻"的整段 postData,
        live_info 的 liveObjectId 被冻结成当时的场次。换场/换天后仍在重放旧 ID,
        接口返回 errCode=0 但 data 里没有 liveStats -> live_stats 永远停在旧值/空值,
        看板的 当前在线 / 累计观看 / GMV / 时长 随之消失(日志无任何报错,极难定位)。
        2026-10-29 对照实验实锤:同一 cookie/headers 只换这个 ID,旧 ID 返回 data 仅
        {multiPullStreamUrlInfos, realtimeStatus},新 ID 返回 14 个 liveStats 字段。

        判定用【解析后的顶层键】而不是字符串包含 —— check_live_status 的 postData 里
        有 getLiveStreamContext 这类嵌套对象,哪天它带上 liveObjectId,字符串判断会把
        字段注入到 check_live_status 的顶层,污染原本正常的请求。

        返回 None 表示"这轮别发":还没有本场 liveObjectId 时,发出去必然是空数据,
        发了反而会把"本轮拿到了 liveStats"这件事变成假象。
        """
        raw = spec.get("postData")
        if not raw:
            return raw
        try:
            obj = json.loads(raw)
        except Exception:
            return raw          # body 不是 JSON(理论上不存在) -> 保持原行为
        if not isinstance(obj, dict) or "liveObjectId" not in obj:
            return raw          # 顶层没有场次字段(如 check_live_status) -> 原样重放
        oid = self.live_object_id
        if not oid:
            return None
        old = obj.get("liveObjectId") or ""
        # rev57b: 同一 (旧, 新) 组合只打一次 —— 否则模板里那个固定的旧 ID 会让每轮
        # 重放都打一条 INFO。换场后 oid 变了,组合跟着变,会重新打一条。
        if old and str(old) != str(oid):
            _pair = (str(old), str(oid))
            if self._oid_logged != _pair:
                self._oid_logged = _pair
                logger.info(f"[live:{self.account_id}] 重放场次 ID 已刷新: {old} -> {oid}")
        obj["liveObjectId"] = str(oid)
        if "timestamp" in obj:
            obj["timestamp"] = str(int(time.time() * 1000))
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    async def _fetch_replay(self, spec):
        """同源重放学到的请求(fetch,credentials:include 自动带 cookie)。返回响应体文本。

        rev57: 不再原样重放 postData,先经 _fresh_post_data 换成本场次 ID。
        """
        body = spec.get("postData")
        if body:
            body = self._fresh_post_data(spec)
            if body is None:
                logger.debug(f"[live:{self.account_id}] 缺本场 liveObjectId,跳过该接口重放")
                return ""
        merged = dict(spec)
        merged["postData"] = body
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
        # rev21: page.evaluate 没有内建超时,页面卡死时会永久挂住整个 poll 循环
        r = await _with_timeout(self.page.evaluate(js, merged), _EVAL_TIMEOUT)
        if r is None:
            logger.debug(f"[live:{self.account_id}] fetch 重放超时/失败")
            return ""
        return r

    async def _enable_cdp_fallback(self):
        """主动轮询失败/学不到 URL 时回退: 重臂 rev11 的 CDP 响应捕获(功能优先,内存次之)。"""
        if self._cdp_fallback:
            return
        try:
            if self._cdp is not None:
                self._cdp.on("Network.responseReceived", self._on_cdp_response)
                self._cdp_fallback = True
                logger.warning(f"[live:{self.account_id}] 直播检测回退到 CDP 响应捕获(主动轮询不可用)")
                # rev43: 被动监听只能看到【之后】发出的响应,而 check_live_status 通常在
                # 页面初始化时就发完了。不 reload 的话这段监听就是空等,必然误判下播。
                if not self._learned:
                    self._schedule_reload("学习超时")
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
        if not stats:
            # rev57: 空 data 是"场次 ID 不对 / 场次已结束 / 接口改版"的唯一表现(接口 errCode=0),
            # 原来静默 return,看板数据消失却毫无线索。限流 5 分钟告警一次。
            _now = time.time()
            if _now - (self._empty_info_warn_ts or 0) > 300:
                self._empty_info_warn_ts = _now
                _keys = sorted(d.keys())[:8] if isinstance(d, dict) else type(d).__name__
                logger.warning(
                    f"[live:{self.account_id}] get_live_info 返回空 data"
                    f"(errCode={data.get('errCode')}, used_id={self.live_object_id}, "
                    f"data_keys={_keys}),liveStats 不更新,"
                    f"沿用上次样本={bool(self.live_stats)}")
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
        # rev57: 记开播时刻(秒),供上层在 liveStats 缺 liveDurationInSeconds 时推算时长。
        # 现网 check_live_status 一定带 createTime(实测三账号均有,且是字符串形如 '1790000000')。
        _ct = d.get("createTime") if isinstance(d, dict) else None
        if _ct:
            try:
                _cti = int(_ct)
                self.live_start_ts = _cti // 1000 if _cti > 10 ** 11 else _cti   # 兼容毫秒
            except Exception:
                pass
        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
        # 若响应里自带 liveStats(直接字段或嵌套在 liveInfo 下),直接采用。
        stats = d.get("liveStats") or (d.get("liveInfo") or {}).get("liveStats")
        if isinstance(stats, dict) and stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
            logger.info(f"[live:{self.account_id}] 从 check_live_status 取到 liveStats"
                        f"(字段数={len(stats)}),is_live 判定可用")
        # rev46: 响应里若有明确的布尔直播标志,记为判据(供上层在无 liveStats 时使用)
        for _k in ("isLive", "isLiving", "isLivingNow"):
            _v = d.get(_k)
            if isinstance(_v, bool):
                if self.live_flag is not _v:
                    logger.info(f"[live:{self.account_id}] check_live_status.{_k}={_v}")
                self.live_flag = _v
                self.updated_at = datetime.now().isoformat()
                self.updated_at_ts = time.time()
                break
        # rev46: 首次把【完整】响应落盘 + 打关键字段摘要(现网没有 get_live_info,
        # 是否在播的判据只能从这里的字段里找)
        if not self._status_body_dumped:
            self._status_body_dumped = True
            try:
                _dump_dir = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_dump_dir, exist_ok=True)
                with io_open(os.path.join(_dump_dir, f"{self.account_id}.json"), "w") as f:
                    f.write(txt)
            except Exception:
                pass
            flag_vals = {k: d.get(k) for k in
                         ("status", "isLive", "isLiving", "isLivingNow", "liveStatus")}
            logger.info(f"[live:{self.account_id}] check_live_status 摘要: {flag_vals}, "
                        f"liveObjectId={d.get('liveObjectId')}, liveId={d.get('liveId')}, "
                        f"coverImg={len(d.get('coverImgList') or [])}项, "
                        f"data_keys={sorted(d.keys())}")
        # rev47: status 纳入统一判据(0=未开播,经三账号完整响应 dump 实证)
        if d.get("status") is not None:
            self.status_num = d.get("status")
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

    # ======================== rev47: 直播页文本判据 ========================

    async def read_dom_state(self, dump=True):
        """读直播大屏页(含 iframe)的可视文本,推断"是否在播"。返回 (state, matched)。

        为什么要 iframe:直播控制台主体在 micro/live/liveBuild 里,wujie 微前端
        常把内容渲染进 iframe,主文档 innerText 可能是空的(与 rev30 二维码同源问题)。
        因此逐 frame 取样再拼起来,最多 4 个 frame、每个 2000 字,开销可控。

        dump=True 时首次把全文落盘 data/live_probe/<账号>_dom.txt —— 关键词表目前
        是"保守猜测"(现网未开播时的页面文案还没核对过),先观测一轮再收紧。
        """
        texts = []
        try:
            frames = list(self.page.frames)[:4] if self.page is not None else []
        except Exception:
            frames = []
        # 取文本的三道处理(每一道都是踩过的坑):
        #   ① 克隆 body 并删掉 style/script/noscript —— 直接 textContent 会把 <style>
        #      里的 CSS 全吃进来(实测 3000 字配额被 CSS 占满,界面文案一个字没取到);
        #   ② innerText 优先(反映可见文本),空则 textContent;
        #   ③ 还空就递归 shadowRoot —— wujie 微前端(直播控制台/发布页)把正文放 Shadow DOM。
        js = r"""() => {
            const clean = (root) => {
                try {
                    const c = root.cloneNode(true);
                    if (c.querySelectorAll) {
                        c.querySelectorAll('style,script,noscript,template,link,meta').forEach(e => e.remove());
                    }
                    return c;
                } catch (e) { return root; }
            };
            const grab = (root) => {
                try {
                    const c = clean(root);
                    const t = c.innerText || c.textContent || '';
                    return t.replace(/\s+/g, ' ').trim();
                } catch (e) { return ''; }
            };
            const parts = [];
            let t = grab(document.body || document.documentElement);
            if (t) parts.push(t);
            if (!parts.length) {
                const walk = (root, depth) => {
                    if (!root || depth > 6 || parts.length > 20) return;
                    const x = grab(root);
                    if (x) parts.push(x);
                    let nodes = [];
                    try { nodes = root.querySelectorAll ? root.querySelectorAll('*') : []; } catch (e) {}
                    for (const el of nodes) {
                        if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                    }
                };
                walk(document, 0);
            }
            return parts.join(' || ').slice(0, 3000);
        }"""
        for fr in frames:
            try:
                r = await _with_timeout(fr.evaluate(js), 8)
                if r:
                    texts.append(r)
            except Exception:
                pass
        txt = " || ".join(texts)
        state, matched = read_dom_state_from_text(txt)
        self.dom_state = state
        self.dom_ts = time.time()
        if dump and not self._dom_dumped:
            self._dom_dumped = True
            try:
                _d = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_d, exist_ok=True)
                with io_open(os.path.join(_d, f"{self.account_id}_dom.txt"), "w") as f:
                    f.write(txt or "<EMPTY: 所有 frame 的文本都是空>")
            except Exception:
                pass
            # 空文本也要打:以前是 if txt 才打,于是"取到空"和"根本没跑"在日志里分不清
            logger.info(f"[live:{self.account_id}] 直播页文本采样({len(txt)}字,"
                        f"frames={len(frames)}): 命中={matched or '无'} "
                        f"状态={'在播' if state is True else ('未播' if state is False else '未知')} "
                        f"| {(txt or '<空>')[:200]}")
        return state, matched

    # ======================== rev11 回退: CDP 响应捕获(仅回退模式启用) ========================

    def _on_cdp_response(self, event):
        """CDP Network.responseReceived 回调(轻量 dict,无 Python Response 对象)。

        仅作 rev13 主动轮询失败时的【回退】捕获: 对 get_live_info / check_live_status
        两个目标 URL 调用 getResponseBody 读体,其余数千请求立即返回。用 create_task 避免
        阻塞 CDP 事件分发线程。
        """
        # rev21: 这是此前 7343 个僵尸 Task 的源头 —— 原实现对每个响应无条件
        # create_task,而 cdp.send 可能永不返回 -> 任务永久悬挂,其协程帧钉住
        # 局部变量(含响应体文本),任务本身也被 set 持有而无法回收。
        # 三道闸: ① requestId 有界去重 ② 在飞任务数上限 ③ 任务套超时并可取消。
        try:
            url = (event.get("response") or {}).get("url", "")
            # rev44: 直播页实测跑在 ls.weixin.qq.com 域下,原判断只认 channels.weixin.qq.com
            # -> 该域下的 check_live_status/get_live_info 响应全被丢弃,回退等于没开。
            if "weixin.qq.com" not in url:
                return
            if "get_live_info" in url:
                target = self._capture_cdp
            elif "check_live_status" in url:
                target = self._capture_live_status_cdp
            else:
                return
            rid = event.get("requestId", "")
            if not rid or rid in self._cdp_seen:
                return
            self._cdp_seen.append(rid)
            if len(self._cdp_tasks) >= _CDP_MAX_INFLIGHT:
                if not self._cdp_drop_warned:
                    self._cdp_drop_warned = True
                    logger.warning(f"[live:{self.account_id}] CDP 回退任务已达上限"
                                   f"({_CDP_MAX_INFLIGHT},疑似 getResponseBody 挂起),后续响应丢弃")
                return
            t = asyncio.create_task(self._run_cdp_capture(target, rid))
            self._cdp_tasks.add(t)
            t.add_done_callback(self._cdp_tasks.discard)
        except Exception:
            pass  # CDP 回调不允许异常上抛

    async def _run_cdp_capture(self, fn, request_id):
        """fire-and-forget 包装:强制超时 + 吞异常(异常不能留在 Task 里不取)。"""
        try:
            await asyncio.wait_for(fn(request_id), _CDP_BODY_TIMEOUT * 2)
        except Exception:
            pass

    async def _read_cdp_body(self, request_id: str) -> str:
        """通过 CDP Network.getResponseBody 读取指定请求的响应体文本(回退模式用)。"""
        try:
            if self._cdp is None:
                return ""
            # rev21: 超时闸 —— 会话异常时这个 send 永不返回,会把任务永久挂住
            result = await asyncio.wait_for(
                self._cdp.send("Network.getResponseBody", {"requestId": request_id}),
                _CDP_BODY_TIMEOUT)
            return (result or {}).get("body", "") or ""
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] CDP getResponseBody 失败: {e}")
            return ""

    async def _capture_cdp(self, request_id: str):
        """get_live_info 响应(CDP 回退版): 读体 -> 解析。"""
        txt = None
        try:
            txt = await self._read_cdp_body(request_id)
            if txt:
                await self._parse_live_info(txt)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] get_live_info 解析失败: {e}")
        finally:
            # rev21: 响应体文本(可达数百 KB,str 不被 gc 跟踪)绝不能随帧常驻
            txt = None

    async def _capture_live_status_cdp(self, request_id: str):
        """check_live_status 响应(CDP 回退版): 读体 -> 解析。"""
        txt = None
        try:
            txt = await self._read_cdp_body(request_id)
            if txt:
                await self._parse_live_status(txt)
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] check_live_status 解析失败: {e}")
        finally:
            txt = None

    # ======================== 关闭清理 ========================

    async def close(self):
        """关闭前清理: 移除所有 CDP 监听 + detach session + 解除 flv 路由 + 断 page 引用。

        rev13: 可能注册过 requestWillBeSent(学习) 与 responseReceived(回退)两种监听,均移除。
        """
        # rev21: 先取消在飞的 CDP 回退任务 —— 否则 close() 后它们仍持有协程帧与响应体,
        # 且会继续往已 detach 的 CDP session 发请求,形成"关不干净"的常驻残留。
        if self._cdp_tasks:
            for t in list(self._cdp_tasks):
                if not t.done():
                    t.cancel()
            self._cdp_tasks.clear()
        self._cdp_seen.clear()
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
        """确定 _aid + _log_finder_id。

        rev50: 优先用账号 dict 里已经存好的值 —— 这两个值每次登录就写回 config，
        不需要再去 liveBuild 的 localStorage 读。原实现把"页面在场"变成了
        dashboard 抓取的前置条件，纯属自找麻烦。
        """
        if self._aid and self._log_finder_id:
            return
        acc = self._account or {}
        if not self._aid:
            self._aid = (acc.get("_aid") or "").strip() or None
        if not self._log_finder_id:
            self._log_finder_id = (acc.get("_log_finder_id") or "").strip() or None
        if self._aid and self._log_finder_id:
            return
        if self.page is None:
            return
        try:
            ids = await _with_timeout(self.page.evaluate("""()=>{
                const raw = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
                const fid = localStorage.getItem('finder_username') || '';
                const unquote = s => s ? s.replace(/^"|"$/g, '') : '';
                return {aid: unquote(raw), fid: unquote(fid)};
            }"""), _EVAL_TIMEOUT)
            if not ids:
                return
            self._aid = self._aid or ids.get("aid")
            self._log_finder_id = self._log_finder_id or ids.get("fid")
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] 读取 localStorage 失败: {e}")

    async def _dashboard_post(self, url, body):
        """取 dashboard 数据。

        rev50: 先走进程内 HTTP(不需要 live_page、不用等 SPA 渲染)，失败回退页面 fetch。
        这三个接口是纯 JSON POST，与浏览器路径唯一的差别是少了跨进程 IPC。
        """
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
        # ---------- 1) HTTP 直连 ----------
        if self._account:
            try:
                j = await wx_http.post_json(
                    self._account, full_url, payload,
                    page_url=DASHBOARD_PAGE_URL, timeout=_EVAL_TIMEOUT)
                if isinstance(j, dict) and not j.get("__err")                         and j.get("errCode") in (0, None):
                    return j
                logger.debug(f"[live:{self.account_id}] dashboard HTTP 不可用: "
                             f"{j.get('__err') or ('errCode=' + str(j.get('errCode')))
                                if isinstance(j, dict) else j}")
            except Exception as e:
                logger.debug(f"[live:{self.account_id}] dashboard HTTP 异常: {e}")
        # ---------- 2) 页面 fetch 回退 ----------
        if self.page is None:
            return None
        js = """async (args) => {
            const r = await fetch(args.url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/json'}, body: JSON.stringify(args.body)});
            return await r.text();
        }"""
        # rev21: 超时闸(页面卡死时 evaluate 永不返回 -> fetch_dashboard_data 整轮悬挂)
        txt = await _with_timeout(
            self.page.evaluate(js, {"url": full_url, "body": payload}), _EVAL_TIMEOUT)
        if not txt:
            return None
        try:
            return json.loads(txt)
        finally:
            # rev20: json 解析失败时异常会带 traceback -> 帧局部变量,会把整份响应文本
            # (可达数 MB)钉到异常对象上。finally 里先解绑再让异常上抛,切断这条引用。
            del txt

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
        # rev20 根因修复:解析后立刻压缩时间序列(每份响应数万个 {ts,value} dict ->
        # 每维度一个求和标量)。下游 metric 只用到整场求和,逐点数据是纯负担。
        # 放在这里 = 最早的时机:大对象活的时间最短,没有机会被别的容器持有。
        return shrink_conv(j.get("data") or {})

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
        # dist 同样可能携带时间序列结构,一并压缩(内部只认 trendingSource/portraitAudience,
        # 没有这两个键时是空操作,零副作用)。
        return shrink_conv(j.get("data") or {})

    async def _fetch_refund_rate(self):
        j = await self._dashboard_post(EC_DATA_SUMMARY_API, {"liveObjectId": self.live_object_id})
        if not j:
            return None
        # dist 同样可能携带时间序列结构,一并压缩(内部只认 trendingSource/portraitAudience,
        # 没有这两个键时是空操作,零副作用)。
        return shrink_conv(j.get("data") or {})

    async def fetch_dashboard_data(self):
        if not self.live_object_id:
            return None
        results = await asyncio.gather(
            self._fetch_conv(), self._fetch_dist(), self._fetch_refund_rate(),
            return_exceptions=True)
        conv = dist = summary = r = None
        try:
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
        finally:
            # rev21 核心:载荷就地消除(第二层)。
            #
            # rev20 只压掉了每条序列的 data 数组(数万个 {ts,value} -> 一个 _sum 标量),
            # 但【骨架本身仍在】:每轮响应留下 ~476 个 {_sum,beginTs,data,dimensions,...}
            # 序列 dict,被某个外部容器(引用链追到 Task 上)永久持有 —— 实测 49h 累积
            # 349 万个 dict / 710MB,占 tracked 内存的 80%。
            #
            # 追持有者成本高且易被容器排序误导(见 .workbuddy/memory 取证教训),
            # 因此改用源头消除:extract_all 只产出标量,之后把三个响应【原地清空】。
            # 清空是【原地】的 —— 无论谁还握着这个 dict 引用,它都只剩一个空壳,
            # 数 MB 的子树当场变成垃圾。持有者是谁已不再重要。
            for _payload in (conv, dist, summary):
                if isinstance(_payload, dict):
                    _payload.clear()
            # rev20: 彻底断开对原始大响应的引用。
            # 原写法只 `del conv, dist, summary`,但 asyncio.gather 的结果列表 results
            # 与循环变量 r 仍持有同一批对象,要等本协程帧销毁才释放;一旦该帧被任何东西
            # (异常 traceback、挂起的 Task)钉住,整批响应就永久驻留 —— 这正是泄漏的
            # 放大器。finally 里显式清空并解绑 results,把释放点提前到 extract_all 之后。
            conv = dist = summary = r = None
            results = None
            del results
        # 原地复用缓存 dict:避免每周期丢弃旧 dict 再新建,减少堆碎片
        if self._dashboard_cache:
            self._dashboard_cache.clear()
            self._dashboard_cache.update(metrics)
        else:
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
            # rev43: 带上当前页面 URL —— 被判"未在直播"时能一眼看出是不是被弹回了 /platform
            "page_url": (self.page.url if self.page is not None else ""),
            "live_flag": self.live_flag,   # rev46: check_live_status 的布尔直播标志
            # rev47: 统一判据的输入项
            "status_num": self.status_num,
            "live_start_ts": self.live_start_ts,   # rev57: 时长兜底用
            "dom_state": self.dom_state,
            "dom_ts": self.dom_ts,
        }

    async def fetch(self):
        """主动轮询一次并返回结果;附带 stream_url 诊断(供日志排查前端无视频时定位)。"""
        await self.poll()
        if not self.stream_url and not self._flv_req_warned:
            self._flv_req_warned = True
            logger.warning(f"[live:{self.account_id}] stream_url 仍为空(live_object_id={self.live_object_id},"
                           f"检查 check_live_status 是否返回 audiencePlayUrl/liveCdnUrl)")
        return await self.snapshot()
