# -*- coding: utf-8 -*-
"""rev57 补丁:frontend/src/pages/LiveScreenPage.tsx(3 处,一次性原子写入)。"""
import io
import sys

P = r"D:\SPHGJ\frontend\src\pages\LiveScreenPage.tsx"
s = io.open(P, encoding="utf-8").read()
orig = s

PATCHES = [
    (
        "7a 新增 onlineCount",
        """  // 画面下部 overlay:当前在线(liveStats)+ 自然流量 + 自然GMV(均来自 dashboardV4 metrics)
  const naturalTraffic = it.metrics?.naturalTraffic
  const naturalGmv = it.metrics?.naturalGmv
""",
        """  // 画面下部 overlay:当前在线 + 自然流量 + 自然GMV(均来自 dashboardV4 metrics)
  const naturalTraffic = it.metrics?.naturalTraffic
  const naturalGmv = it.metrics?.naturalGmv
  // rev57: 当前在线优先取 dashboardV4 的 onlineWatchUv(与 9 格指标同源、有 liveObjectId 就能拿),
  // liveStats 只作兜底 —— liveStats 断供时原来会直接显示假 0,看起来像"人都跑光了"。
  const onlineCount = it.metrics?.onlineWatchUv ?? s?.currentOnlineCount
""",
    ),
    (
        "7b overlay 第一格",
        """            <span className="text-xl font-bold tabular-nums">{s?.currentOnlineCount ?? 0}</span>""",
        """            <span className="text-xl font-bold tabular-nums">{onlineCount ?? "-"}</span>""",
    ),
    (
        "7c fmtDuration 缺失显示 -",
        """function fmtDuration(sec?: number) {
  if (!sec) return "0:00"
""",
        """function fmtDuration(sec?: number) {
  // rev57: 缺失(undefined/null)显示 "-",不能落到下面的 0:00 —— 那是"直播刚开始 0 秒"
  // 与"没有数据"两种含义的混淆,用户看到 0:00 会以为数据坏了却没线索。
  if (sec == null) return "-"
  if (!sec) return "0:00"
""",
    ),
]

for tag, old, new in PATCHES:
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次(应为 1),中止,未写盘")
        sys.exit(1)
    s = s.replace(old, new, 1)
    print(f"[ok] {tag}")

io.open(P, "w", encoding="utf-8", newline="").write(s)
print(f"\n已写回 {P}  ({len(orig)} -> {len(s)} 字符)")

s2 = io.open(P, encoding="utf-8").read()
CHECKS = [
    ("onlineCount 定义", "const onlineCount = it.metrics?.onlineWatchUv ?? s?.currentOnlineCount" in s2),
    ("overlay 用 onlineCount", '{onlineCount ?? "-"}' in s2),
    ("不再有 ?? 0 假值", "{s?.currentOnlineCount ?? 0}" not in s2),
    ("fmtDuration 判 null", 'if (sec == null) return "-"' in s2),
]
print()
bad = 0
for name, ok in CHECKS:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad += 0 if ok else 1
print("\nSELFCHECK", "PASS" if bad == 0 else f"FAIL({bad})")
sys.exit(1 if bad else 0)
