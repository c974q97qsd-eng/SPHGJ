# -*- coding: utf-8 -*-
"""rev43 单测:不起浏览器,用 FakePage 验证直播检测的三条关键分支。

覆盖:
  1) 只学到 live_status(缺 live_info)也必须主动重放出数 —— 这是 2 号事故的直接成因
  2) 一个都没学到 -> 超时回退 CDP + 自动 reload 一次(否则被动监听空等)
  3) 规格落盘 -> 新实例从缓存继承,首轮 poll 即出数(不必重学)
"""
import asyncio
import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import live_fetcher as lfmod
from backend.live_fetcher import LiveFetcher, _load_spec_cache, _save_spec_cache

# 缓存写到临时文件,别污染真实 data/
lfmod._SPEC_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_tmp_live_api_spec.json")
if os.path.exists(lfmod._SPEC_CACHE_PATH):
    os.remove(lfmod._SPEC_CACHE_PATH)

STATUS_BODY = json.dumps({
    "data": {"liveObjectId": "15041301292406212857",
             "audiencePlayUrl": "//wxlivecdn.example.com/abc.flv"}
}, ensure_ascii=False)
INFO_BODY = json.dumps({"data": {"liveStats": {"totalAudienceCount": 1234}}}, ensure_ascii=False)

SPEC_STATUS = {"url": "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/live/check_live_status",
               "method": "POST", "headers": {"Cookie": "SECRET=1", "X-WECHAT": "1"},
               "postData": "{}"}


class FakePage:
    def __init__(self, url="https://channels.weixin.qq.com/platform/live/liveBuild"):
        self.url = url
        self.reloads = 0
        self.evals = 0

    async def evaluate(self, js, spec):
        self.evals += 1
        return STATUS_BODY if "check_live_status" in (spec or {}).get("url", "") else INFO_BODY

    async def reload(self, wait_until=None):
        self.reloads += 1
        return None

    def is_closed(self):
        return False


async def case1_partial_spec():
    """只学到 live_status:必须主动重放出数(is_live 信号拿得到)"""
    page = FakePage()
    lf = LiveFetcher(page, "t_partial")
    lf._learned = {"live_status": dict(SPEC_STATUS)}
    lf._learning = False
    lf._learn_deadline = 0.0     # 学习窗口早过了,模拟"只学到一半"的实例
    info = await lf.poll()
    ok = bool(lf.live_object_id == "15041301292406212857" and lf.stream_url
              and info.get("stream_url"))
    print(f"  1) 只学到 live_status -> live_object_id={lf.live_object_id}, "
          f"stream_url={'有' if lf.stream_url else '无'}, cdp_fallback={lf._cdp_fallback} "
          f"-> {'PASS' if ok else 'FAIL'}")
    return ok


async def case2_no_spec_reload():
    """一个都没学到:超时回退 CDP,并自动 reload 一次"""
    page = FakePage()
    lf = LiveFetcher(page, "t_nospec")
    lf._learned = {}
    lf._learning = True
    lf._learn_deadline = 0.0     # 立刻超时
    lf._cdp = _FakeCdp()
    await lf.poll()
    await asyncio.sleep(0.2)     # 等后台 reload 任务跑完
    ok = lf._cdp_fallback and page.reloads == 1 and lf._reload_nudged
    print(f"  2) 无规格超时 -> cdp_fallback={lf._cdp_fallback}, "
          f"reload={page.reloads} 次 -> {'PASS' if ok else 'FAIL'}")
    return ok


async def case3_persist_and_inherit():
    """规格落盘 -> 新实例从缓存继承,首轮 poll 出数;且 cookie 不落盘"""
    _save_spec_cache("t_persist", {"live_status": dict(SPEC_STATUS)})
    raw = open(lfmod._SPEC_CACHE_PATH, encoding="utf-8").read()
    cached = _load_spec_cache("t_persist")
    page = FakePage()
    lf = LiveFetcher(page, "t_persist")      # 不传 learned,应自动读缓存
    info = await lf.poll()
    ok = (cached and "Cookie" not in raw and "SECRET" not in raw
          and lf.live_object_id == "15041301292406212857" and info.get("stream_url"))
    print(f"  3) 从磁盘缓存继承 -> 命中={sorted(lf._learned)}, "
          f"cookie 未落盘={'SECRET' not in raw}, live_object_id={lf.live_object_id} "
          f"-> {'PASS' if ok else 'FAIL'}")
    return ok


class _FakeCdp:
    def __init__(self):
        self.listeners = []

    def on(self, ev, fn):
        self.listeners.append((ev, fn))

    def remove_listener(self, ev, fn):
        self.listeners = [(e, f) for e, f in self.listeners if (e, f) != (ev, fn)]


async def case4_relearn():
    """relearn():重载页面 + 重开学习窗口"""
    page = FakePage()
    lf = LiveFetcher(page, "t_relearn")
    lf._learned = {}
    lf._cdp = _FakeCdp()
    r = await lf.relearn()
    armed = any(ev == "Network.requestWillBeSent" for ev, _ in lf._cdp.listeners)
    ok = bool(r and page.reloads == 1 and lf._learning and armed)
    print(f"  4) relearn -> reload={page.reloads}, learning={lf._learning}, "
          f"监听重臂={armed} -> {'PASS' if ok else 'FAIL'}")
    return ok


async def main():
    results = [
        await case1_partial_spec(),
        await case2_no_spec_reload(),
        await case3_persist_and_inherit(),
        await case4_relearn(),
    ]
    print()
    print("ALL PASS" if all(results) else "SOME FAIL")
    # 清理测试缓存
    try:
        os.remove(lfmod._SPEC_CACHE_PATH)
    except Exception:
        pass
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
