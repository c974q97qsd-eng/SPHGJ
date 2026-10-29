# -*- coding: utf-8 -*-
"""rev57 决定性验证:get_live_info 到底还返不返 liveStats?

背景:母本 run.log 24 小时内「取到 liveStats」0 次、「(来自 get_live_info)」0 次,
说明这个请求一次都没出过数。但无法区分是
  A) 场次 ID 冻在旧场次 -> 接口 errCode=0 但 data 空(rev57 诊断的根因1)
  B) 接口本身已不给 liveStats(诊断第六节的"诊断分支")

本脚本用【当天真实 liveObjectId】(从 live_probe/check_live_status 响应里取)
+ 账号真实 cookie 各发一次,直接看返回。只读查询接口,不改任何状态。

用法:
    python tools/probe_rev57f.py            # 只测在播账号
    python tools/probe_rev57f.py --all      # 全部有 cookie 的账号
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


def probe_id_of(acc):
    """该账号当天 check_live_status 返回的真实 liveObjectId(没有则 None)。"""
    p = os.path.join(PROBE, f"{acc}.json")
    if not os.path.isfile(p):
        return None, None
    try:
        d = jload(p).get("data") or {}
    except Exception:
        return None, None
    return d.get("liveObjectId"), d.get("status")


async def one(acc_dict, learned_headers, oid, status):
    aid = (acc_dict.get("_aid") or "").strip()
    fid = (acc_dict.get("_log_finder_id") or "").strip()
    url = f"{LIVE_API}?_aid={aid}"
    body = wx_http.std_body(fid, liveObjectId=str(oid))
    j = await wx_http.post_json(acc_dict, url, body, page_url=PAGE,
                               headers=learned_headers, timeout=15)
    if not isinstance(j, dict):
        return f"非 dict: {j!r}"[:160]
    if j.get("__err"):
        return f"__err={j['__err']} code={j.get('code')} raw={str(j.get('raw'))[:80]}"
    d = j.get("data")
    if not isinstance(d, dict):
        return (f"errCode={j.get('errCode')} errMsg={j.get('errMsg')!r} "
                f"data={type(d).__name__}/{d!r}")
    st = d.get("liveStats")
    keys = sorted(d)
    if isinstance(st, dict) and st:
        return (f"errCode={j.get('errCode')} ★liveStats 有! 字段{len(st)}个: "
                f"{sorted(st)[:16]}")
    return (f"errCode={j.get('errCode')} liveStats=无 data_keys({len(keys)})={keys[:14]}")


async def main():
    only_live = "--all" not in sys.argv
    cfg = jload(CFG)
    spec = jload(SPEC)
    print(f"HTTP 后端: {wx_http.backend_name()}")
    print("=" * 78)
    for a in (cfg.get("accounts") or []):
        acc = a.get("id")
        ss = a.get("storage_state") or {}
        if not (ss.get("cookies")):
            print(f"\n[{acc}] 跳过:无 cookie(未登录)")
            continue
        oid, status = probe_id_of(acc)
        if not oid:
            print(f"\n[{acc}] 跳过:无 check_live_status 探针(拿不到真实场次 ID)")
            continue
        if only_live and str(status) != "1":
            print(f"\n[{acc}] 跳过:探针 status={status}(未在播)")
            continue
        hdr = {}
        # 带上"学到"的原始 headers(去掉 cookie),这些指纹头可能必需
        for s in (spec.get(acc) or {}).values():
            if s.get("url") and "get_live_info" in s.get("url", ""):
                hdr = {k: v for k, v in (s.get("headers") or {}).items()
                       if k.lower() != "cookie"}
                break
        print(f"\n[{acc}] status={status} 真实 liveObjectId={oid}")
        print(f"  headers 带入: {sorted(hdr)}")
        try:
            r = await one(a, hdr, oid, status)
        except Exception as e:
            r = f"异常 {e!r}"[:160]
        print(f"  -> {r}")
    await wx_http.aclose()


asyncio.run(main())
