"""直播大屏指标字典:把 dashboardV4 三 API 响应映射成可配置的指标 key。

每个 Metric = {key, display_name, unit, format, group, extract(conv, dist, summary)}
- conv    = get_ec_conversion_dashboard_data_v3 的 data
- dist    = getLiveDistributionChannel 的 data
- summary = get_live_ec_data_summary 的 data
- format: int / float / currency(值=分,前端 ÷100 显 ¥) / percent(值=0~100,前端显 x%)
          / duration(值=秒,前端显时分秒)
- 所有 dashboardV4 字段(get_live_info/liveStats 不在此列,需求:卡片仅 dashboardV4)。
"""
import logging
logger = logging.getLogger("sphgj")


def _num(v):
    """转数字:str 数字/数字 -> int 或 float;bool/空/失败返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
    return None


def _overview(conv, key):
    return _num(((conv or {}).get("overview") or {}).get(key))


def _summary(summary, key):
    return _num((summary or {}).get(key))


def _conv_field(conv, key):
    return _num(((conv or {}).get("conversionAnalysis") or {}).get(key))


def _pct(v):
    """小数 -> 已乘 100 的百分比(round 2)。None 透传。"""
    if v is None:
        return None
    return round(v * 100, 2)


def _sum_new_watch(conv, label):
    """trendingSource.newWatchUv 里 dimensions value=label 的整场 sum。"""
    arr = ((conv or {}).get("trendingSource") or {}).get("newWatchUv") or []
    for item in arr:
        dims = item.get("dimensions") or []
        if len(dims) != 1:
            continue
        lab = dims[0].get("value") or dims[0].get("uxLabel") or ""
        if lab == label:
            return sum(_num(x.get("value")) or 0 for x in (item.get("data") or []))
    return None


def _gender_ratio(conv, label):
    """portraitAudience.onlineWatchUv 性别维度(type 3)label 占比 %(已乘 100)。"""
    arr = ((conv or {}).get("portraitAudience") or {}).get("onlineWatchUv") or []
    counts = {}
    for item in arr:
        dims = item.get("dimensions") or []
        if len(dims) != 1 or str(dims[0].get("type")) != "3":
            continue
        lab = dims[0].get("value") or dims[0].get("uxLabel") or ""
        counts[lab] = counts.get(lab, 0) + sum(_num(x.get("value")) or 0 for x in (item.get("data") or []))
    total = sum(counts.values())
    if total > 0:
        return round(counts.get(label, 0) / total * 100, 2)
    return None


def _dist_gmv(dist, channel_type):
    """liveDistChannelSourceStats 里 newLiveDstChannelType=channel_type 的 gmv(分)。"""
    for s in (dist or {}).get("liveDistChannelSourceStats") or []:
        if s.get("newLiveDstChannelType") == channel_type:
            return _num(s.get("gmv"))
    return None


def _natural_gmv_ratio(dist):
    """公域 gmv / 总 gmv * 100(已乘 100)。"""
    total = 0
    public = 0
    for s in (dist or {}).get("liveDistChannelSourceStats") or []:
        g = _num(s.get("gmv")) or 0
        total += g
        if s.get("newLiveDstChannelType") == 1:
            public = g
    return round(public / total * 100, 2) if total > 0 else None


def _heat_gmv_per_1000(conv, dist):
    """加热千次观看成交,返回分 = 加热 gmv(分) ÷ 加热进房 × 1000。前端 currency ÷100 显元。"""
    heat_gmv = _dist_gmv(dist, 2)
    heat_uv = _sum_new_watch(conv, "直播加热")
    if heat_gmv and heat_uv:
        return round(int(heat_gmv) / heat_uv * 1000, 2)
    return None


class Metric:
    __slots__ = ("key", "display_name", "unit", "format", "group", "extract")

    def __init__(self, key, display_name, unit, fmt, group, extract):
        self.key = key
        self.display_name = display_name
        self.unit = unit
        self.format = fmt
        self.group = group
        self.extract = extract


def _m(key, name, fmt, group, extract, unit=""):
    return Metric(key, name, unit, fmt, group, extract)


METRICS = [
    # ---- overview(get_ec_conversion.data.overview,整场汇总)----
    _m("cumulativeWatchUv", "累计观看人次", "int", "overview", lambda c, d, s: _overview(c, "cumulativeWatchUv")),
    _m("cumulativeWatchPv", "累计观看次数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeWatchPv")),
    _m("onlineWatchUv", "当前在线", "int", "overview", lambda c, d, s: _overview(c, "onlineWatchUv")),
    _m("peakOnlineWatchUv", "峰值在线", "int", "overview", lambda c, d, s: _overview(c, "peakOnlineWatchUv")),
    _m("averageOnlineWatchUvPerMinute", "分钟均在线", "int", "overview", lambda c, d, s: _overview(c, "averageOnlineWatchUvPerMinute")),
    _m("averageWatchSecondsPerAudience", "人均观看时长", "duration", "overview", lambda c, d, s: _overview(c, "averageWatchSecondsPerAudience")),
    _m("cumulativeNewFollowUv", "累计新增关注", "int", "overview", lambda c, d, s: _overview(c, "cumulativeNewFollowUv")),
    _m("cumulativeNewFansClubUv", "新增粉丝团", "int", "overview", lambda c, d, s: _overview(c, "cumulativeNewFansClubUv")),
    _m("cumulativeCommentUv", "评论人数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeCommentUv")),
    _m("cumulativeCommentPv", "评论次数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeCommentPv")),
    _m("cumulativeSharingUv", "分享人数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeSharingUv")),
    _m("cumulativeSharingPv", "分享次数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeSharingPv")),
    _m("cumulativeLikePv", "点赞次数", "int", "overview", lambda c, d, s: _overview(c, "cumulativeLikePv")),
    _m("likeUv", "点赞人数", "int", "overview", lambda c, d, s: _overview(c, "likeUv")),
    _m("impressionUv", "曝光人数", "int", "overview", lambda c, d, s: _overview(c, "impressionUv")),
    _m("impressionPv", "曝光次数", "int", "overview", lambda c, d, s: _overview(c, "impressionPv")),
    _m("promotionCumulativeWatchPv", "商品观看次数", "int", "overview", lambda c, d, s: _overview(c, "promotionCumulativeWatchPv")),
    # ---- conversion(get_ec_conversion.data.conversionAnalysis)----
    _m("convNewWatchUv", "进房人次", "int", "conversion", lambda c, d, s: _conv_field(c, "newWatchUv")),
    _m("convNewWatchPv", "进房次数", "int", "conversion", lambda c, d, s: _conv_field(c, "newWatchPv")),
    _m("convImpressionUv", "曝光人数", "int", "conversion", lambda c, d, s: _conv_field(c, "impressionUv")),
    _m("convImpressionPv", "曝光次数", "int", "conversion", lambda c, d, s: _conv_field(c, "impressionPv")),
    # ---- traffic(trendingSource.newWatchUv 整场 sum)----
    _m("naturalTraffic", "公域进房", "int", "traffic", lambda c, d, s: _sum_new_watch(c, "公域流量")),
    _m("heatUv", "加热进房", "int", "traffic", lambda c, d, s: _sum_new_watch(c, "直播加热")),
    # ---- portrait(性别维度占比)----
    _m("maleRatio", "男性占比", "percent", "portrait", lambda c, d, s: _gender_ratio(c, "男性")),
    _m("femaleRatio", "女性占比", "percent", "portrait", lambda c, d, s: _gender_ratio(c, "女性")),
    # ---- dist(getLiveDistributionChannel)----
    _m("naturalGmv", "公域成交占比", "percent", "dist", lambda c, d, s: _natural_gmv_ratio(d)),
    _m("publicGmv", "公域成交金额", "currency", "dist", lambda c, d, s: _dist_gmv(d, 1)),
    _m("heatGmv", "加热成交金额", "currency", "dist", lambda c, d, s: _dist_gmv(d, 2)),
    _m("heatGmvPer1000", "加热千次成交", "currency", "dist", lambda c, d, s: _heat_gmv_per_1000(c, d)),
    # ---- summary(get_live_ec_data_summary)----
    _m("totalGmv", "总成交", "currency", "summary", lambda c, d, s: _summary(s, "totalGmv")),
    _m("totalPayUv", "支付人数", "int", "summary", lambda c, d, s: _summary(s, "totalPayUv")),
    _m("totalPayPv", "支付笔数", "int", "summary", lambda c, d, s: _summary(s, "totalPayPv")),
    _m("totalClkUv", "点击人数", "int", "summary", lambda c, d, s: _summary(s, "totalClkUv")),
    _m("totalClkPv", "点击次数", "int", "summary", lambda c, d, s: _summary(s, "totalClkPv")),
    _m("customerPrice", "客单价", "currency", "summary", lambda c, d, s: _summary(s, "customerPrice")),
    _m("audiencePayRatio", "观众支付率", "percent", "summary", lambda c, d, s: _pct(_summary(s, "audiencePayRatio"))),
    _m("clkPayRatio", "点击支付率", "percent", "summary", lambda c, d, s: _pct(_summary(s, "clkPayRatio"))),
    _m("newBuyerUv", "新买家", "int", "summary", lambda c, d, s: _summary(s, "newBuyerUv")),
    _m("refundRate", "退款率", "percent", "summary", lambda c, d, s: _pct(_summary(s, "refundRate"))),
    _m("refundAmount", "退款金额", "currency", "summary", lambda c, d, s: _summary(s, "refundAmount")),
    _m("estimateCommission", "预估佣金", "currency", "summary", lambda c, d, s: _summary(s, "estimateCommission")),
    _m("cumulativeAudienceCount", "累计观众", "int", "summary", lambda c, d, s: _summary(s, "cumulativeAudienceCount")),
    # ---- local(非 dashboard API 字段;extract 返回 None,真值由 account_manager 并入 info["metrics"])----
    _m("audience_10m", "10分钟观看增值", "int", "local", lambda c, d, s: None),
    _m("gmv_10m", "10分钟GMV增值", "yuan", "local", lambda c, d, s: None),
    _m("gmv_30m", "30分钟GMV增值", "yuan", "local", lambda c, d, s: None),
    _m("liveDuration", "直播时长", "duration", "local", lambda c, d, s: None),  # live_stats.liveDurationInSeconds
]

ALL_METRICS = {m.key: m for m in METRICS}


def metric_dictionary():
    """前端配置 UI:key/display_name/unit/format/group。"""
    return [{"key": m.key, "display_name": m.display_name, "unit": m.unit,
             "format": m.format, "group": m.group} for m in METRICS]


def extract_all(conv, dist, summary):
    """从三 API 的 data dict 提取全部指标 -> {key: value}。提取异常/无值写 None。"""
    out = {}
    for m in METRICS:
        try:
            v = m.extract(conv, dist, summary)
        except Exception as e:
            logger.debug(f"指标 {m.key} 提取失败: {e}")
            v = None
        out[m.key] = v
    return out


# 默认 9 项卡片(全 dashboardV4,覆盖流量/成交/转化/人群)
DEFAULT_CARD_FIELDS = [
    "cumulativeWatchUv",   # 累计观看
    "liveDuration",        # 直播时长(本地)
    "totalGmv",            # GMV(总成交)
    "audience_10m",        # 10分钟观看(增值,本地)
    "gmv_10m",             # 10分钟GMV(增值,本地)
    "gmv_30m",             # 30分钟GMV(增值,本地)
    "refundRate",          # 退款率
    "maleRatio",           # 男性占比
    "heatGmvPer1000",      # 加热千次成交
]


def validate_card_fields(fields):
    """校验卡片配置:正好 9 个、key 都在字典里、去重。返回 (ok, err)。"""
    if not isinstance(fields, list) or len(fields) != 9:
        return False, "必须正好选 9 个指标"
    if len(set(fields)) != 9:
        return False, "指标不能重复"
    for k in fields:
        if k not in ALL_METRICS:
            return False, f"未知指标: {k}"
    return True, None
