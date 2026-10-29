# -*- coding: utf-8 -*-
"""rev57 对照实验:同一账号、同一套 cookie/headers,只换 liveObjectId。

  A组 = live_api_spec.json 里缓存的(冻结在"学到那一刻")的旧场次 ID
  B组 = check_live_status 当天返回的真实场次 ID

预期: A组 errCode=0 但 data 里没有 liveStats(静默空手);B组有 14 个字段。
这就把「根因1」从推测变成实锤 —— 接口没改版,是我们在拿旧场次 ID 打。
只读查询接口,不改任何状态。
"""
import asyncio
import io
import json
import os
import sys

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http          # noqa: E402

PROBE = r"\\Sphgj\sphgj\data\live_probe"
CFG = r"\\Sphgj\sphgj\config.json"
SPEC = r"\\Sphgj\sphgj\data\live_api_spec.json"
LIVE_API = ("https://channels.weixin.qq.com/micro/live/cgi-bin/"
            "mmfinderassistant-bin/live/get_live_info")
PAGE = "https://channels.weixin.qq.com/micro/live/liveBuild"


def jload(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return json.load(f)


async def call(acc, hdr, oid):
    aid = (acc.get("_aid") or "").strip()
    fid = (acc.get("_log_finder_id") or "").strip()
    body = wx_http.std_body(fid, liveObjectId=str(oid))
    j = await wx_http.post_json(acc, f"{LIVE_API}?_aid={aid}", body,
                               page_url=PAGE, headers=hdr, timeout=15)
    if not isinstance(j, dict) or j.get("__err"):
        return f"__err={j.get('__err') if isinstance(j, dict) else j}"
    d = j.get("data")
    if not isinstance(d, dict):
        return f"errCode={j.get('errCode')} data={type(d).__name__}"
    st = d.get("liveStats")
    if isinstance(st, dict) and st:
        return (f"errCode={j.get('errCode')}  liveStats★ {len(st)} 字段  "
                f"currentOnline={st.get('currentOnlineCount')} "
                f"dur={st.get('liveDurationInSeconds')} "
                f"gmv={st.get('payedGmv')}")
    return f"errCode={j.get('errCode')}  liveStats=无  data_keys={sorted(d)[:8]}"


async def main():
    cfg = jload(CFG)
    spec = jload(SPEC)
    print(f"HTTP 后端: {wx_http.backend_name()}")
    for a in (cfg.get("accounts") or []):
        acc = a.get("id")
        if not ((a.get("storage_state") or {}).get("cookies")):
            continue
        pp = os.path.join(PROBE, f"{acc}.json")
        if not os.path.isfile(pp):
            continue
        d = (jload(pp).get("data") or {})
        real = d.get("liveObjectId")
        if not real or str(d.get("status")) != "1":
            continue
        li = (spec.get(acc) or {}).get("live_info") or {}
        try:
            frozen = str(json.loads(li.get("postData") or "{}").get("liveObjectId") or "")
        except Exception:
            frozen = ""
        hdr = {k: v for k, v in (li.get("headers") or {}).items()
               if k.lower() != "cookie"}
        print("\n" + "=" * 78)
        print(f"[{acc}]")
        print(f"  A 冻结ID {frozen!r}")
        print(f"    -> {await call(a, hdr, frozen) if frozen else '(缓存里没有)'}")
        print(f"  B 真实ID {real!r}")
        print(f"    -> {await call(a, hdr, real)}")
    await wx_http.aclose()


asyncio.run(main())
