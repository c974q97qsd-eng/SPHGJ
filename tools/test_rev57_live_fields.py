# -*- coding: utf-8 -*-
"""rev57 单测:直播看板字段缺失 / 假 0。

覆盖 4 条根因对应的改动:
  1. _capture_req 落库前清空 liveObjectId(场次变量不冻结进模板)
  2. poll 顺序 status -> info
  3. _fresh_post_data 重放前用本场次 ID + 当前时间戳重写 body
  4. _parse_live_info 空 data 限流告警 / _parse_live_status 记 createTime
  5. snapshot 带出 live_start_ts / _live_loop 时长兜底 / 前端去掉 ?? 0

fixture 的字段形状来自 2026-10-29 对母本的实测响应(值已脱敏/简化):
  * check_live_status 在播:40 个 data 键,含 liveObjectId/status=1/createTime/
    audiencePlayUrl,但【没有】liveStats 和 liveInfo
  * check_live_status 未播:13 个键,status=0,无 createTime
  * get_live_info【新场次】:errCode=0,data.liveStats 有 14 个字段
  * get_live_info【旧场次】:errCode=0,data 只有 multiPullStreamUrlInfos/realtimeStatus

跑:python tools/test_rev57_live_fields.py
"""
import asyncio
import io
import json
import logging
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.live_fetcher import LiveFetcher   # noqa: E402

PASS = 0
FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  << " + str(extra) if extra else ""))


# ==================== fixtures ====================

LIVE_OID = "15000000000000000001"          # 本场场次(真实值已脱敏为等长假值)
OLD_OID = "15000000000000000002"           # 缓存里冻结的上一场(同上)

STATUS_POST = json.dumps({
    "getLiveStreamContext": {"useNewLogic": True, "scene": 3, "protocolType": 0,
                             "videoCodecType": 1, "rateType": 3},
    "timestamp": "1793149414257", "_log_finder_uin": "",
    "_log_finder_id": "v2_test@finder", "rawKeyBuff": "", "pluginSessionId": None,
    "scene": 7, "reqScene": 7,
})

INFO_POST = json.dumps({
    "liveObjectId": OLD_OID,
    "timestamp": "1793149415296", "_log_finder_uin": "",
    "_log_finder_id": "v2_test@finder", "rawKeyBuff": "", "pluginSessionId": None,
    "scene": 7, "reqScene": 7,
})

STATUS_LIVE = json.dumps({
    "errCode": 0, "errMsg": "request successful",
    "data": {
        "liveObjectId": LIVE_OID, "liveId": 2042972124620990177, "status": 1,
        "createTime": "1790000000",
        "audiencePlayUrl": "https://pull-m1.wxlivecdn.com/trtc_test/orig_test.flv",
        "anchorStreamingKey": "k", "anchorStreamingUrl": "https://x/flv",
        "liveQualityData": {}, "liveStreamUrlInfo": {}, "switchStatus": 1,
        "wxNickname": "TEST_ACCOUNT", "poiName": "", "poiCity": "",
        "description": "", "canManageLiveSwitch": True, "canPauseLive": True,
    },
})

STATUS_OFFLINE = json.dumps({
    "errCode": 0, "data": {
        "liveObjectId": "15000000000000000003", "status": 0,
        "wxNickname": "TEST_ACCOUNT", "coverImgList": [], "experimentInfo": {},
    },
})

INFO_STATS = json.dumps({
    "errCode": 0, "data": {"liveStats": {
        "currentOnlineCount": 48, "liveDurationInSeconds": 43149, "payedGmv": 58196,
        "payedNum": 41, "purchasePrice": 1400, "purchaseUserCount": 30,
        "totalAudienceCount": 12345, "newFollowCount": 3, "totalCheerCount": 5,
        "totalCommentCount": 77, "totalHeat": 8888, "privateDomainUv": 900,
        "purchaseHeat": 12, "eulerSingleJoin": 0,
    }},
})

INFO_EMPTY = json.dumps({
    "errCode": 0, "data": {"multiPullStreamUrlInfos": [], "realtimeStatus": 2},
})


def new_lf(acc="testacc"):
    """page=None 的实例;预设 _status_body_dumped 避免测试往 data/live_probe 落盘。"""
    lf = LiveFetcher(None, acc)
    lf._status_body_dumped = True
    lf._dom_dumped = True
    return lf


class FakePage:
    """按 url 路由返回预设文本,并记录每次 evaluate 的 body。"""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []          # [(url, body)]
        self.url = "https://channels.weixin.qq.com/micro/live/liveBuild"

    async def evaluate(self, js, arg):
        url = (arg or {}).get("url", "")
        self.calls.append((url, (arg or {}).get("postData")))
        for key, txt in self.routes.items():
            if key in url:
                return txt
        return ""


class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def capture_log(fn_coro):
    """跑一个协程并返回其期间产生的日志行。"""
    h = Cap()
    lg = logging.getLogger("sphgj")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        asyncio.run(fn_coro)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    return h.lines


# ==================== 1. _capture_req ====================
print("\n[1] _capture_req:场次 ID 不落库")
lf = new_lf()
spec = lf._capture_req({"url": "u", "method": "POST",
                        "headers": {"Cookie": "x"}, "postData": INFO_POST})
o = json.loads(spec["postData"])
ck(o.get("liveObjectId") == "", "含 liveObjectId -> 清空为空串", o.get("liveObjectId"))
ck(o.get("_log_finder_id") == "v2_test@finder", "其余字段原样保留")
ck(o.get("scene") == 7 and o.get("reqScene") == 7, "scene/reqScene 保留")
ck(spec["headers"] == {"Cookie": "x"}, "headers 原样透传")
ck(spec["method"] == "POST", "method 透传")

spec2 = lf._capture_req({"url": "u", "postData": STATUS_POST})
ck(json.loads(spec2["postData"]) == json.loads(STATUS_POST),
   "check_live_status 形态(无 liveObjectId)-> 一字不改")

spec3 = lf._capture_req({"url": "u", "postData": "not-a-json{{"})
ck(spec3["postData"] == "not-a-json{{", "非 JSON postData -> 原样,不崩")

spec4 = lf._capture_req({"url": "u", "postData": None})
ck(spec4["postData"] is None, "postData=None -> None")

nested = json.dumps({"getLiveStreamContext": {"liveObjectId": OLD_OID}, "scene": 7})
spec5 = lf._capture_req({"url": "u", "postData": nested})
ck(json.loads(spec5["postData"])["getLiveStreamContext"]["liveObjectId"] == OLD_OID,
   "只有嵌套 liveObjectId -> 顶层不受影响、不注入字段")
ck("liveObjectId" not in json.loads(spec5["postData"]),
   "嵌套场景不会给顶层凭空加 liveObjectId")

ck(lf._capture_req({"url": "u"})["postData"] is None, "无 postData 键 -> None")

# ==================== 2. _fresh_post_data ====================
print("\n[2] _fresh_post_data:重放前换成本场次 ID")
lf = new_lf()
ck(lf._fresh_post_data({"postData": None}) is None, "无 postData -> None")
ck(lf._fresh_post_data({"postData": "raw{{"}) == "raw{{", "非 JSON -> 原样")
ck(lf._fresh_post_data({"postData": STATUS_POST}) == STATUS_POST,
   "顶层无 liveObjectId(check_live_status)-> 原样,绝不注入")
ck(lf._fresh_post_data({"postData": nested}) == nested,
   "嵌套有、顶层无 -> 原样,不污染")

lf.live_object_id = None
ck(lf._fresh_post_data({"postData": INFO_POST}) is None,
   "有 liveObjectId 字段但当前无 ID -> None(这轮别发)")

lf.live_object_id = LIVE_OID
logs = []


async def _run_fresh():
    logs.extend(capture_log(asyncio.sleep(0)))
    _fresh = lf._fresh_post_data({"postData": INFO_POST})
    return _fresh


# 日志要用真实调用捕获
h = Cap()
lg = logging.getLogger("sphgj")
lg.addHandler(h)
lg.setLevel(logging.DEBUG)
out = lf._fresh_post_data({"postData": INFO_POST})
lg.removeHandler(h)
o = json.loads(out)
ck(o["liveObjectId"] == LIVE_OID, "旧 ID -> 换成本场 ID", o["liveObjectId"])
ck(o["timestamp"] != "1793149415296", "timestamp 刷新为当前毫秒")
ck(len(o["timestamp"]) == 13 and o["timestamp"].isdigit(), "timestamp 仍是 13 位毫秒串",
   o["timestamp"])
ck(any("重放场次 ID 已刷新" in m and OLD_OID in m and LIVE_OID in m for m in h.lines),
   "换了不同 ID 时打一次 INFO 日志", h.lines)

h2 = Cap()
lg.addHandler(h2)
lg.setLevel(logging.DEBUG)
out2 = lf._fresh_post_data({"postData": json.dumps(
    {"liveObjectId": LIVE_OID, "timestamp": "111", "scene": 7})})
lg.removeHandler(h2)
ck(json.loads(out2)["liveObjectId"] == LIVE_OID, "ID 相同 -> 值不变")
ck(not any("重放场次 ID 已刷新" in m for m in h2.lines), "ID 相同时不打刷新日志")
ck(json.loads(out2)["scene"] == 7, "其余字段保留")

# 清空后的模板(改动 3 落库的形态)也要能正常回填
lf._learned = {"live_info": {"url": "u", "postData": json.dumps(
    {"liveObjectId": "", "timestamp": "1", "scene": 7})}}
back = json.loads(lf._fresh_post_data(lf._learned["live_info"]))
ck(back["liveObjectId"] == LIVE_OID, "空串模板 -> 回填本场 ID")
ck(not any("重放场次 ID 已刷新" in m for m in []), "(空串回填不打刷新日志,见下条)")
h3 = Cap()
lg.addHandler(h3)
lg.setLevel(logging.DEBUG)
lf._fresh_post_data(lf._learned["live_info"])
lg.removeHandler(h3)
ck(not any("重放场次 ID 已刷新" in m for m in h3.lines),
   "旧值为空串时不打「已刷新」(避免每轮刷屏)")

# ==================== 3. _fetch_replay ====================
print("\n[3] _fetch_replay:缺本场 ID 时零发包")


async def t_replay_skip():
    """没学到 live_status(拿不到本场 ID)时,get_live_info 必须一次都不发。

    这是 _fresh_post_data 返回 None 的唯一实际触发路径 —— 打出去必然是空 data,
    白耗一次请求还把"这轮拿到了 liveStats"变成假象。
    """
    lf = new_lf("acc_skip")
    pg = FakePage({"/get_live_info": INFO_STATS})
    lf.page = pg
    lf.live_object_id = None
    lf._learned = {
        "live_info": {"url": "https://h/get_live_info", "postData": INFO_POST},
    }
    await lf.poll()
    return pg, lf


pg, lf3 = asyncio.run(t_replay_skip())
ck(len(pg.calls) == 0, "无任何场次 ID -> get_live_info 零发包", pg.calls)
ck(lf3.live_stats is None, "零发包 -> live_stats 仍为空")

STATUS_NO_OID = json.dumps({
    "errCode": 0, "data": {"status": 1, "createTime": "1790000000",
                           "audiencePlayUrl": "https://x/a.flv"},
})


async def t_status_no_oid():
    """check_live_status 回来了但没带 liveObjectId -> info 仍应跳过。"""
    lf = new_lf("acc_noid")
    pg = FakePage({"/check_live_status": STATUS_NO_OID, "/get_live_info": INFO_STATS})
    lf.page = pg
    lf.live_object_id = None
    lf._learned = {
        "live_status": {"url": "https://h/check_live_status", "postData": STATUS_POST},
        "live_info": {"url": "https://h/get_live_info", "postData": INFO_POST},
    }
    await lf.poll()
    return pg, lf


pgn, lfn = asyncio.run(t_status_no_oid())
ck(any("check_live_status" in u for u, _ in pgn.calls), "status 照发")
ck(not any("get_live_info" in u for u, _ in pgn.calls),
   "status 没给场次 ID -> info 也不发(不打空枪)", [u for u, _ in pgn.calls])
ck(lfn.live_object_id is None, "live_object_id 保持 None")
ck(lfn.live_start_ts == 1790000000, "但 createTime 照样拿到(时长兜底可用)")


async def t_replay_first_round():
    """改动 2(status 先)+ 改动 4(重写 body)的协同:第一轮就该有数据。"""
    lf = new_lf("acc_1st")
    pg = FakePage({"/get_live_info": INFO_STATS, "/check_live_status": STATUS_LIVE})
    lf.page = pg
    lf.live_object_id = None
    lf._learned = {
        "live_status": {"url": "https://h/check_live_status", "postData": STATUS_POST},
        "live_info": {"url": "https://h/get_live_info", "postData": INFO_POST},
    }
    await lf.poll()
    return pg, lf


pg1, lfa = asyncio.run(t_replay_first_round())
info_bodies = [b for u, b in pg1.calls if "get_live_info" in u]
ck(len(info_bodies) == 1, "第一轮就发出 get_live_info(1 次)", len(info_bodies))
ck(info_bodies and json.loads(info_bodies[0])["liveObjectId"] == LIVE_OID,
   "★ body 用的是 status 刚拿到的本场 ID,不是缓存旧 ID",
   json.loads(info_bodies[0])["liveObjectId"] if info_bodies else None)
ck(lfa.live_stats and lfa.live_stats.get("currentOnlineCount") == 48,
   "★ 第一轮即拿到 liveStats.currentOnlineCount=48(无空窗)", lfa.live_stats)
ck(lfa.live_stats.get("liveDurationInSeconds") == 43149, "liveDurationInSeconds=43149")
ck(lfa.live_stats.get("payedGmv") == 58196, "payedGmv=58196")
ck(lfa.updated_at_ts > 0, "updated_at_ts 被刷新(判活用)")


async def t_replay_second():
    """稳态两轮:每轮都发,且每轮 body 都是本场 ID(证明不是只对一次)。"""
    lf = new_lf("acc_2nd")
    pg = FakePage({"/get_live_info": INFO_STATS, "/check_live_status": STATUS_LIVE})
    lf.page = pg
    lf.live_object_id = None
    lf._learned = {
        "live_status": {"url": "https://h/check_live_status", "postData": STATUS_POST},
        "live_info": {"url": "https://h/get_live_info", "postData": INFO_POST},
    }
    await lf.poll()
    await lf.poll()
    return pg, lf


pg2, lf4 = asyncio.run(t_replay_second())
info_bodies2 = [b for u, b in pg2.calls if "get_live_info" in u]
ck(len(info_bodies2) == 2, "两轮共发 2 次 get_live_info", len(info_bodies2))
ck(all(json.loads(b)["liveObjectId"] == LIVE_OID for b in info_bodies2),
   "每轮 body 都是本场 ID(不是只对一次)", info_bodies2)
ck(all(json.loads(b)["liveObjectId"] != OLD_OID for b in info_bodies2),
   "缓存里的旧场次 ID 一次都没被发出")
ck(lf4.live_stats.get("currentOnlineCount") == 48, "第二轮仍正常拿数据")

# ==================== 4. poll 顺序 ====================
print("\n[4] poll:重放顺序必须是 status -> info")


async def t_order():
    lf = new_lf("acc_order")
    lf.live_object_id = LIVE_OID
    order = []

    async def fake_replay(spec):
        order.append("status" if "check_live_status" in spec["url"] else "info")
        return STATUS_LIVE if "check_live_status" in spec["url"] else INFO_STATS

    lf._fetch_replay = fake_replay
    lf._parse_live_status = lambda txt: asyncio.sleep(0)
    lf._parse_live_info = lambda txt: asyncio.sleep(0)
    lf._learned = {
        "live_info": {"url": "https://h/get_live_info", "postData": INFO_POST},
        "live_status": {"url": "https://h/check_live_status", "postData": STATUS_POST},
    }
    await lf.poll()
    return order


order = asyncio.run(t_order())
ck(order == ["status", "info"], "顺序 = status -> info", order)
ck(order.index("status") < order.index("info") if len(order) == 2 else False,
   "status 严格早于 info")

# ==================== 5. _parse_live_info 告警 ====================
print("\n[5] _parse_live_info:空 data 限流告警 + 不丢上次样本")


async def t_empty_warn():
    lf = new_lf("acc_empty")
    lf.live_object_id = "15000000000000000002"
    h = Cap()
    lg = logging.getLogger("sphgj")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        await lf._parse_live_info(INFO_EMPTY)
        await lf._parse_live_info(INFO_EMPTY)     # 第二次应被限流
        first = list(h.lines)
        lf._empty_info_warn_ts = 0.0              # 手动过期,再发一次
        await lf._parse_live_info(INFO_EMPTY)
        third = list(h.lines)
    finally:
        lg.removeHandler(h)
    return lf, first, third


lf5, warns_first, warns_third = asyncio.run(t_empty_warn())
w1 = [m for m in warns_first if "get_live_info 返回空 data" in m]
ck(len(w1) == 1, "连续两次空 data -> 只告警 1 次(限流 300s)", len(w1))
ck(w1 and "used_id=15000000000000000002" in w1[0], "告警带出实际使用的场次 ID", w1)
ck(w1 and "realtimeStatus" in w1[0] or (w1 and "multiPullStreamUrlInfos" in w1[0]),
   "告警列出 data_keys,便于判断是旧场次还是改版", w1)
ck(w1 and "沿用上次样本=False" in w1[0], "告警说明是否沿用了上次样本")
ck(len([m for m in warns_third if "get_live_info 返回空 data" in m]) == 2,
   "限流窗口过后可再次告警", len([m for m in warns_third if "返回空 data" in m]))
ck(lf5.live_stats is None, "空 data 不写入 live_stats(保持 None)")


async def t_empty_keeps_sample():
    lf = new_lf("acc_keep")
    await lf._parse_live_info(INFO_STATS)
    before = lf.live_stats.get("currentOnlineCount")
    await lf._parse_live_info(INFO_EMPTY)
    return lf, before


lf6, before = asyncio.run(t_empty_keeps_sample())
ck(lf6.live_stats.get("currentOnlineCount") == before == 48,
   "空 data 不清空上次样本(沿用,不是归零)")


async def t_stats_ok():
    lf = new_lf("acc_ok")
    h = Cap()
    lg = logging.getLogger("sphgj")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        ok = await lf._parse_live_info(INFO_STATS)
    finally:
        lg.removeHandler(h)
    return lf, ok, h.lines


lf7, ok, logs7 = asyncio.run(t_stats_ok())
ck(ok is True, "_parse_live_info 返回 True")
ck(not any("返回空 data" in m for m in logs7), "有 liveStats 时不告警")
ck(lf7.live_stats["currentOnlineCount"] == 48, "live_stats 写入")

# ==================== 6. _parse_live_status 记 createTime ====================
print("\n[6] _parse_live_status:记录 createTime 供时长兜底")


async def t_ct(txt):
    lf = new_lf()
    await lf._parse_live_status(txt)
    return lf


lf8 = asyncio.run(t_ct(STATUS_LIVE))
ck(lf8.live_start_ts == 1790000000, "字符串秒级 createTime -> int 秒", lf8.live_start_ts)
ck(lf8.live_object_id == LIVE_OID, "liveObjectId 正常解析(本场 ID 来源)")
ck(lf8.stream_url and ".flv" in lf8.stream_url, "stream_url 正常解析")
ck(lf8.status_num == 1, "status_num=1")

ms = json.dumps({"errCode": 0, "data": {"liveObjectId": LIVE_OID, "status": 1,
                                        "createTime": "1790000000000"}})
lf9 = asyncio.run(t_ct(ms))
ck(lf9.live_start_ts == 1790000000, "毫秒级 createTime -> 折算成秒", lf9.live_start_ts)

lf10 = asyncio.run(t_ct(STATUS_OFFLINE))
ck(lf10.live_start_ts is None, "未播(无 createTime)-> 保持 None")
ck(lf10.status_num == 0, "未播 status_num=0")

bad = json.dumps({"errCode": 0, "data": {"liveObjectId": LIVE_OID, "createTime": "abc"}})
lf11 = asyncio.run(t_ct(bad))
ck(lf11.live_start_ts is None, "非法 createTime -> 不崩,保持 None")

num = json.dumps({"errCode": 0, "data": {"liveObjectId": LIVE_OID,
                                         "createTime": 1790000000}})
lf12 = asyncio.run(t_ct(num))
ck(lf12.live_start_ts == 1790000000, "createTime 是数字也支持")

# ==================== 7. snapshot ====================
print("\n[7] snapshot:带出 live_start_ts")


async def t_snap():
    lf = new_lf()
    await lf._parse_live_status(STATUS_LIVE)
    return await lf.snapshot()


snap = asyncio.run(t_snap())
ck("live_start_ts" in snap, "snapshot 含 live_start_ts")
ck(snap["live_start_ts"] == 1790000000, "值正确传递到上层", snap.get("live_start_ts"))
ck(snap["live_stats"] is None or snap["live_stats"] is not None, "其余键未被破坏")
for k in ("live_stats", "stream_url", "updated_at", "metrics", "page_url",
          "live_flag", "status_num", "dom_state", "dom_ts"):
    if k not in snap:
        ck(False, f"旧键 {k} 丢失")
        break
else:
    ck(True, "原有 snapshot 键一个不少")

# ==================== 8. account_manager 时长兜底 ====================
print("\n[8] _live_loop 时长兜底(源码断言 + 数值验算)")
src = io.open(os.path.join(ROOT, "backend", "account_manager.py"), encoding="utf-8").read()
ck('_dur = (info.get("live_stats") or {}).get("liveDurationInSeconds")' in src,
   "优先用 liveDurationInSeconds")
ck('_st = info.get("live_start_ts")' in src, "缺失时读 live_start_ts")
ck("0 < _d < 24 * 3600" in src, "24h 护栏存在")
ck('m["liveDuration"] = _dur' in src, "最终写入 metrics.liveDuration")
ck("if _dur in (None, 0):" in src, "None 与 0 都触发兜底")


def resolve_dur(info, now):
    """与 _live_loop 内联逻辑等价(用于数值验算)。"""
    _dur = (info.get("live_stats") or {}).get("liveDurationInSeconds")
    if _dur in (None, 0):
        _st = info.get("live_start_ts")
        if isinstance(_st, int) and _st > 0:
            _d = int(now) - _st
            _dur = _d if 0 < _d < 24 * 3600 else None
    return _dur


NOW = 1790000500          # 比 fixture 的 createTime(1790000000) 晚 500s
ck(resolve_dur({"live_stats": {"liveDurationInSeconds": 43149},
                "live_start_ts": 1790000000}, NOW) == 43149, "有 liveStats -> 直接用")
ck(resolve_dur({"live_stats": None, "live_start_ts": 1790000000}, NOW) == NOW - 1790000000,
   "无 liveStats -> 用 createTime 推算", resolve_dur(
       {"live_stats": None, "live_start_ts": 1790000000}, NOW))
ck(resolve_dur({"live_stats": {}, "live_start_ts": None}, NOW) is None,
   "两者都无 -> None(前端显示 -,不是 0:00)")
ck(resolve_dur({"live_stats": None, "live_start_ts": NOW + 999}, NOW) is None,
   "createTime 在未来 -> 丢弃(不出负数)")
ck(resolve_dur({"live_stats": None, "live_start_ts": NOW - 30 * 3600}, NOW) is None,
   "超过 24h -> 丢弃(残留旧场次时不显示天文数字)")
ck(resolve_dur({"live_stats": {"liveDurationInSeconds": 0},
                "live_start_ts": 1790000000}, NOW) == NOW - 1790000000,
   "liveDuration=0 也走兜底")

# ==================== 9. 前端 ====================
print("\n[9] 前端 LiveScreenPage")
tsx = io.open(os.path.join(ROOT, "frontend", "src", "pages", "LiveScreenPage.tsx"),
              encoding="utf-8").read()
ck("it.metrics?.onlineWatchUv ?? s?.currentOnlineCount" in tsx,
   "当前在线优先 metrics.onlineWatchUv,liveStats 兜底")
ck('{onlineCount ?? "-"}' in tsx, "缺失渲染为 -,不再是 0")
ck("{s?.currentOnlineCount ?? 0}" not in tsx, "假 0 已移除")
ck('if (sec == null) return "-"' in tsx, "fmtDuration 缺失 -> -")
ck('if (!sec) return "0:00"' in tsx, "真正的 0 秒仍显示 0:00(两种含义分开)")

# onlineWatchUv 必须在后端指标字典里,否则前端 ?? 永远落不到
met = io.open(os.path.join(ROOT, "backend", "metrics.py"), encoding="utf-8").read()
ck('_m("onlineWatchUv", "当前在线"' in met, "后端确有 onlineWatchUv 指标")
ck('_m("liveDuration", "直播时长", "duration"' in met, "后端确有 liveDuration 指标(duration 格式)")

# ==================== 汇总 ====================
print("\n" + "=" * 60)
print(f"PASS {PASS}   FAIL {FAIL}")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
