# -*- coding: utf-8 -*-
"""rev57 诊断核对第三弹:从母本 run.log 还原「反复重建 / 判未播」的真实链路。

关注 4 个问题:
  1) LiveFetcher 重建频率(「继承已学 API 规格」条数按小时)
  2) judge_live 的结论分布(「直播信号中断」/「非直播,关闭 live_page」)
  3) get_live_info 有没有任何一次出数
  4) 19:41 之后那段「22 分钟无 [live:*]」期间到底发生了什么
"""
import io
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec  # noqa: E402

SRC = r"\\Sphgj\sphgj\run.log"
raw = open(SRC, "rb").read()
lines = [dec(x) for x in raw.splitlines()]

# 只保留最近 8 小时
pat_ts = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")
recent = []
for ln in lines:
    m = pat_ts.match(ln)
    if m:
        recent.append((f"{m.group(1)}:{m.group(2)}", ln))
print(f"总行数 {len(lines)},带时间戳 {len(recent)}")

KEYS = [
    ("继承已学API", "继承已学 API 规格"),
    ("重建live_page", "live_page 定期 close+重建"),
    ("信号中断", "直播信号中断"),
    ("关页非直播", "非直播,关闭 live_page"),
    ("重载重试捕获", "重载 live_page 重试捕获"),
    ("取到liveStats", "取到 liveStats"),
    ("来自live_info的id", "(来自 get_live_info)"),
    ("dashboard全失败", "dashboard 抓取全失败"),
    ("场次刷新", "重放场次 ID 已刷新"),
    ("空data", "返回空 data"),
]

print("\n" + "=" * 76)
print("按小时 × 关键事件计数")
print("=" * 76)
by_hour = defaultdict(Counter)
for hm, ln in recent:
    h = hm[:2]
    for label, needle in KEYS:
        if needle in ln:
            by_hour[h][label] += 1
labels = [k for k, _ in KEYS]
print("时段   " + " ".join(f"{l:>14}" for l in labels))
for h in sorted(by_hour):
    print(f"{h}:xx  " + " ".join(f"{by_hour[h][l]:>14}" for l in labels))

print("\n" + "=" * 76)
print("19:35 ~ 20:10 逐条(只保留判活/重建/关页/账号级日志)")
print("=" * 76)
show = ("直播信号中断", "非直播,关闭", "继承已学 API", "重载 live_page",
        "定期 close+重建", "取到 liveStats", "直播检测", "探测", "开播",
        "check_live_status 摘要", "dashboard 抓取全失败")
for hm, ln in recent:
    if not ("19:35" <= hm or hm.startswith("20:0") or hm.startswith("20:1")):
        continue
    if not ("19:3" in hm[:3] or "19:4" in hm[:3] or "19:5" in hm[:3]
            or hm.startswith("20:0") or hm.startswith("20:1")):
        continue
    if any(s in ln for s in show):
        # 摘要行很长,截断
        print(ln[:190])
