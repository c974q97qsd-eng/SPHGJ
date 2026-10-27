"""按播放增长给作品分区,决定每一轮到底要检查哪些视频的评论。

用户 2026-10-28 定的规则(原话):
    「热区视频不按时间定义,按我们抓取的作品播放数最近 3 日的增长来判断热区普通区
      以及全量,定义每账号前 30 个为热区,普通区为前 80 个」

为什么不用 createTime 分区:新发布的视频未必有互动,而老视频可能因为直播间导流/
爆款回流突然涨播放和评论。播放数增长才是「这个视频最近真的有人在看」的直接证据。

分区口径:
    rank 1~30   热区(HEAT_TOP)     每轮都检查评论
    rank 31~80  普通区(NORMAL_TOP) 每 NORMAL_EVERY 轮检查一次
    rank 81+    冷区               每 COLD_EVERY 轮全量扫时检查

排序依据(降级链,保证任何数据条件下都能出确定结果):
    1. growth = 本次 readCount - 3 日前的 readCount(主口径)
    2. growth 缺失(新视频 / 采样历史不足 3 天)→ 用 readCount 绝对值
    3. readCount 也缺失 → 保持 post_list 原顺序(倒序 = 最新优先)

为什么要有降级链:上线第一天 video_activity 是空表,如果硬等 3 天数据才敢分区,
这三天就等于没有热区扫描。降级后第一天就能工作,3 天后自动切到主口径。
"""
import logging

logger = logging.getLogger("sphgj")

HEAT_TOP = 30
NORMAL_TOP = 80

ZONE_HEAT = "heat"
ZONE_NORMAL = "normal"
ZONE_COLD = "cold"


def _read_count(v):
    """取播放数。字段可能缺失/非数字,统一成 int 或 None(区分「0」与「没有」)。"""
    raw = v.get("readCount")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def classify(videos, growth=None, heat_top=HEAT_TOP, normal_top=NORMAL_TOP):
    """把一批视频按活跃度分区。

    videos: post_list 顺序的列表,[{"objectId":.., "readCount":.., "createTime":..}]
    growth: {objectId: 3 日播放增长}(可缺项,缺项走降级链)

    返回 dict:
        ordered  [oid]                    按活跃度降序
        zones    {oid: heat|normal|cold}
        basis    growth|read|order        本次实际用的排序依据(便于日志核对)
        counts   {heat:n, normal:n, cold:n}
    """
    growth = growth or {}
    items = []
    for idx, v in enumerate(videos or []):
        oid = v.get("objectId")
        if not oid:
            continue
        items.append({
            "oid": oid,
            "idx": idx,
            "read": _read_count(v),
            "grow": growth.get(oid),          # None = 无基线
        })
    if not items:
        return dict(ordered=[], zones={}, basis="order", counts={})

    has_growth = any(it["grow"] is not None for it in items)
    has_read = any(it["read"] is not None for it in items)
    if has_growth:
        basis = "growth"
        # 有增长的排前面并按增长降序;没有基线的(新视频)按播放数插在中间:
        # 用 -1 作为"未知增长"的替身,保证它们不会被无脑压到冷区。
        key = lambda it: (-(it["grow"] if it["grow"] is not None else -1),
                          -(it["read"] or 0), it["idx"])
    elif has_read:
        basis = "read"
        key = lambda it: (-(it["read"] or 0), it["idx"])
    else:
        basis = "order"
        key = lambda it: it["idx"]

    items.sort(key=key)
    zones, ordered = {}, []
    counts = {ZONE_HEAT: 0, ZONE_NORMAL: 0, ZONE_COLD: 0}
    for rank, it in enumerate(items, start=1):
        if rank <= heat_top:
            z = ZONE_HEAT
        elif rank <= normal_top:
            z = ZONE_NORMAL
        else:
            z = ZONE_COLD
        zones[it["oid"]] = z
        ordered.append(it["oid"])
        counts[z] += 1
    return dict(ordered=ordered, zones=zones, basis=basis, counts=counts)


def wanted(zones, full=False, include_normal=False):
    """本轮需要检查评论的视频集合(oid 集合)。

    full=True        全量扫:三个区都查(每 COLD_EVERY 轮一次)
    include_normal   普通区轮:热区 + 普通区
    否则              热区轮:只查热区
    """
    if full:
        return {oid for oid, z in zones.items()
                if z in (ZONE_HEAT, ZONE_NORMAL, ZONE_COLD)}
    keep = {ZONE_HEAT}
    if include_normal:
        keep.add(ZONE_NORMAL)
    return {oid for oid, z in zones.items() if z in keep}


def describe(result, full=False, include_normal=False):
    """一行日志文案,便于在 run.log 里核对分区是否按预期滚动。"""
    c = result.get("counts") or {}
    mode = "全量" if full else ("普通区" if include_normal else "热区")
    return (f"分区({mode}, 依据={result.get('basis')}): "
            f"热{c.get(ZONE_HEAT,0)} 普{c.get(ZONE_NORMAL,0)} 冷{c.get(ZONE_COLD,0)}")
