# -*- coding: utf-8 -*-
"""rev57 运行期验证:重启后的真实效果。

检查 6 件事:
  1) 启动日志 0 ERROR
  2) 出现「重放场次 ID 已刷新」(改动 2+4 生效,已用本场次 ID)
  3) 不再出现「get_live_info 返回空 data」(或最多一次,且 used_id 是本场 ID)
  4) 「直播信号中断」不再刷屏(旧代码每小时 100~390 条)
  5) /api/live-screen/status 里在播账号的 currentOnlineCount 非空
  6) metrics.onlineWatchUv 有值(前端 overlay 的新数据源)
"""
import io
import json
import re
import sys
import time
import urllib.request

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec          # noqa: E402

BASE = "http://192.168.1.27:8712"
LOG = r"\\Sphgj\sphgj\run.log"


def api(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


print("=" * 72)
print("[5/6] 看板接口")
print("=" * 72)
try:
    st = api("/api/live-screen/status")
except Exception as e:
    print(f"接口失败: {e!r}")
    sys.exit(1)

items = st.get("items") if isinstance(st, dict) else st
if isinstance(items, dict):
    items = list(items.values())
items = items or []
print(f"账号数: {len(items)}")

live = [x for x in items if x.get("is_live")]
print(f"在播: {len(live)} 个\n")

hdr = f"{'账号':<16}{'is_live':<8}{'在线':>6}{'累计观看':>10}{'GMV(分)':>10}{'时长':>8}  {'metrics键'}"
print(hdr)
print("-" * 72)
for x in items:
    acc = str(x.get("account_id") or x.get("name") or "")[:15]
    ls = x.get("live_stats") or {}
    m = x.get("metrics") or {}
    dur = m.get("liveDuration")
    print(f"{acc:<16}{str(x.get('is_live')):<8}"
          f"{str(ls.get('currentOnlineCount')):>6}"
          f"{str(ls.get('totalAudienceCount')):>10}"
          f"{str(ls.get('payedGmv')):>10}"
          f"{str(dur):>8}  {len(m)}键")

print()
ok_cnt = 0
for x in live:
    acc = x.get("account_id")
    ls = x.get("live_stats") or {}
    m = x.get("metrics") or {}
    bad = []
    if ls.get("currentOnlineCount") is None:
        bad.append("live_stats.currentOnlineCount 空")
    if m.get("onlineWatchUv") is None:
        bad.append("metrics.onlineWatchUv 空")
    if not bad:
        ok_cnt += 1
    print(f"  {'PASS' if not bad else 'FAIL'}  {acc}: " +
          ("liveStats + dashboard 都有值" if not bad else " / ".join(bad)))

print()
print("=" * 72)
print("[1~4] run.log(只看本次启动之后)")
print("=" * 72)
raw = open(LOG, "rb").read()
lines = [dec(x) for x in raw.splitlines()]

# 定位本次启动:最后一次出现启动横幅
start = 0
for i in range(len(lines) - 1, -1, -1):
    if "[启动]" in lines[i] or "视频号工具" in lines[i]:
        start = i
        break
tail = lines[start:]
print(f"本次启动起点: 第 {start} 行 / 共 {len(lines)} 行,之后 {len(tail)} 行")

txt = "\n".join(tail)


def cnt(needle):
    return txt.count(needle)


errs = [ln for ln in tail if " ERROR " in ln]
print(f"\nERROR 行: {len(errs)}")
for ln in errs[:5]:
    print("   " + ln[:150])

checks = [
    ("重放场次 ID 已刷新", "重放场次 ID 已刷新", 1, "改动 2+4 生效,已在用本场次 ID"),
    ("get_live_info 返回空 data", "get_live_info 返回空 data", 0,
     "不再空手(出现说明场次 ID 或接口仍有问题)"),
    ("直播信号中断", "直播信号中断", 0, "不再误判下播(旧代码每小时上百条)"),
    ("非直播,关闭 live_page", "非直播,关闭 live_page", 0, "不再反复关页"),
    ("回退到 CDP", "回退到 CDP", 0, "主动轮询正常,没退到高内存模式"),
]
print()
allok = True
for label, needle, limit, why in checks:
    n = cnt(needle)
    ok = (n >= limit) if limit > 0 else (n <= limit)
    allok = allok and ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {n} 条  —— {why}")

# 拿到的 liveStats 样本
print()
samples = [ln for ln in tail if "check_live_status 摘要" in ln][:2]
for ln in samples:
    m = re.search(r"liveObjectId=(\d+)", ln)
    print(f"  最新 check_live_status: liveObjectId={m.group(1) if m else '?'}")

print("\n" + "=" * 72)
print(f"SUMMARY  接口在播 {len(live)} 个 / 字段齐全 {ok_cnt} 个  |  日志项 "
      f"{'全 PASS' if allok else '有 FAIL'}")
print("=" * 72)
