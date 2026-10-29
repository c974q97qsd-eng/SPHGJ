# -*- coding: utf-8 -*-
"""等 dashboardV4 指标出数(轮询而不是盲等)。

dashboard 间隔 60s + 单次最多 45s 超时,所以最多等 150s。
一旦有账号的 metrics 非空就报告并退出。
"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"


def st():
    with urllib.request.urlopen(BASE + "/api/live-screen/status", timeout=10) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    items = d.get("items") if isinstance(d, dict) else d
    return items or []


t0 = time.time()
last = {}
while time.time() - t0 < 160:
    items = st()
    for x in items:
        if not x.get("is_live"):
            continue
        acc = x.get("account_id")
        m = x.get("metrics") or {}
        last[acc] = len(m)
        if m:
            print(f"\n{int(time.time()-t0)}s  {acc} metrics 出数: {len(m)} 键")
            for k in sorted(m):
                print(f"   {k} = {m[k]}")
            # 关键字段
            print("\n关注字段:")
            for k in ("onlineWatchUv", "cumulativeWatchUv", "totalGmv", "liveDuration",
                      "naturalTraffic", "naturalGmv", "audience_10m", "gmv_10m", "gmv_30m"):
                print(f"   {k:>18} = {m.get(k)!r}")
            sys.exit(0)
    time.sleep(5)

print(f"\n160s 内 metrics 仍为空。各在播账号 metrics 键数: {last}")
print("说明 dashboardV4 抓取没成功 —— 需要看 fetch_dashboard_data 的原因。")
sys.exit(1)
