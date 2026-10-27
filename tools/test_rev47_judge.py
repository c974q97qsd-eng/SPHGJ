# -*- coding: utf-8 -*-
"""rev47 单测:统一判据 + DOM 文本 + 轻量探测解析链路

不启浏览器,纯逻辑验证:
  1. judge_live 优先级矩阵(6 条规则、sticky stream_url、fresh 窗口)
  2. DOM 关键词(保守:拿不准必须 None)
  3. 用母本真实 dump 的 check_live_status 响应走一遍轻量探测的解析路径
     (LiveFetcher(page=None) + _parse_live_status),确认与大屏页探测同结论
  4. Playwright 是否支持 BrowserContext.request(轻量探测的技术前提)
"""
import json
import sys
import asyncio

sys.path.insert(0, ".")
from backend.live_fetcher import judge_live, read_dom_state_from_text, LiveFetcher

ok = fail = 0


def chk(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  OK   {name} -> {got}")
    else:
        fail += 1
        print(f"  FAIL {name} -> got={got} want={want}")


print("== 1. judge_live 优先级 ==")
chk("live_stats 新鲜", judge_live(live_stats={"a": 1}, stats_fresh=True)[0], True)
chk("live_stats 过期", judge_live(live_stats={"a": 1}, stats_fresh=False, status=0)[0], False)
chk("DOM 在播", judge_live(dom_state=True)[0], True)
chk("DOM 未开播", judge_live(dom_state=False)[0], False)
chk("stream_url(允许)", judge_live(stream_url="https://x/a.flv", allow_stream_url=True)[0], True)
chk("stream_url(sticky 禁用)", judge_live(stream_url="https://x/a.flv", allow_stream_url=False)[0], False)
chk("status=0", judge_live(status=0)[0], False)
chk("什么都没有", judge_live()[0], False)
chk("DOM 过期不算", judge_live(dom_state=True, dom_fresh=False, status=0)[0], False)
chk("live_stats 压过 status=0", judge_live(live_stats={"a": 1}, status=0)[0], True)
chk("判据原因可读", judge_live(status=0)[1], "status=0")
chk("DOM 压过 stream_url", judge_live(dom_state=False, stream_url="x.flv")[0], False)

print("\n== 2. DOM 关键词(保守) ==")
chk("未开播文案", read_dom_state_from_text("今天还没开始 未开播 去设置")[0], False)
chk("直播中文案", read_dom_state_from_text("正在直播 在线人数 128")[0], True)
chk("空文本", read_dom_state_from_text("")[0], None)
chk("无关文本必须 None", read_dom_state_from_text("小店 商品 数据概览")[0], None)
chk("未开播优先于直播中", read_dom_state_from_text("未开播（直播中奖活动已结束）")[0], False)

print("\n== 3. 轻量探测解析(母本真实 dump) ==")
REAL_NOT_LIVE = {
    "errCode": 0,
    "errMsg": "ok",
    "data": {
        "status": 0,
        "liveObjectId": "15041633472574196092",
        "liveId": "2042971579164087660",
        "coverImgList": [],
        "switchStatus": 0,
        "rewardAvailable": 0,
        "wxNickname": "U.S.POLOASSN.免税仓",
        "operatorRole": 1,
        "liveLogoAvailable": False,
        "isLiveRequiredDescription": "",
        "experimentInfo": {"finderAssistantLiveConsoleV2": 1},
    },
}
REAL_LIVE = {
    "errCode": 0,
    "data": {
        "status": 1,
        "liveObjectId": "15041633472574196092",
        "audiencePlayUrl": "//finderlive.wxlivecdn.com/x/live.flv?k=1",
        "liveStats": {"totalAudienceCount": 500, "payedGmv": 1200},
    },
}


async def parse(body):
    lf = LiveFetcher(None, "test_account")
    lf.live_object_id = "-"
    lf._status_body_dumped = True
    await lf._parse_live_status(json.dumps(body, ensure_ascii=False))
    return judge_live(live_stats=lf.live_stats, stats_fresh=True,
                      stream_url=lf.stream_url, allow_stream_url=True,
                      status=lf.status_num)


r1 = asyncio.run(parse(REAL_NOT_LIVE))
chk("真实未开播响应 -> 未播", r1[0], False)
chk("真实未开播响应的依据", r1[1], "status=0")
lfx = LiveFetcher(None, "test_account")
lfx._status_body_dumped = True
asyncio.run(lfx._parse_live_status(json.dumps(REAL_NOT_LIVE, ensure_ascii=False)))
chk("解析出 status", lfx.status_num, 0)
chk("解析出 liveObjectId", lfx.live_object_id, "15041633472574196092")
chk("未开播时没有 stream_url", lfx.stream_url, None)
chk("未开播时没有 live_stats", lfx.live_stats, None)
chk("page=None 不崩", True, True)

r2 = asyncio.run(parse(REAL_LIVE))
chk("合成在播响应 -> 在播", r2[0], True)
chk("合成在播响应的依据", r2[1], "live_stats")
lfy = LiveFetcher(None, "test_account")
lfy._status_body_dumped = True
asyncio.run(lfy._parse_live_status(json.dumps(REAL_LIVE, ensure_ascii=False)))
chk("在播时解析出 stream_url",
    bool(lfy.stream_url and "wxlivecdn" in lfy.stream_url), True)

print("\n== 4. Playwright 能力前提 ==")
try:
    from playwright.async_api import BrowserContext
    chk("BrowserContext 有 request(APIRequestContext)",
        hasattr(BrowserContext, "request"), True)
except Exception as e:
    chk(f"导入 BrowserContext 失败: {e}", False, True)

print(f"\n结果: {ok} 通过 / {fail} 失败")
assert fail == 0, "有用例失败"
print("ALL PASS")
