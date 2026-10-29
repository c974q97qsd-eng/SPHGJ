# -*- coding: utf-8 -*-
"""rev57b 验证:等采集跑够轮次,统计「重放场次 ID 已刷新」是否已去重。

判定:每个在播账号在本次启动后只应有 1 条(换场时才会再来一条)。
修复前是【每轮一条】—— 4 账号约 1800 条/小时。
"""
import json
import sys
import time
import urllib.request
from collections import Counter

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec          # noqa: E402

BASE = "http://192.168.1.27:8712"
LOG = r"\\Sphgj\sphgj\run.log"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def items():
    d = api("/api/live-screen/status")
    x = d.get("items") if isinstance(d, dict) else d
    return x or []


# 等至少 100 秒(约 12 轮 poll),让日志积累足够样本
print("等采集跑够轮次 ...")
t0 = time.time()
first = {}
while time.time() - t0 < 130:
    time.sleep(10)
    cur = {x.get("account_id"): x.get("updated_at") for x in items() if x.get("is_live")}
    if not first:
        first = dict(cur)
    elapsed = int(time.time() - t0)
    print(f"  {elapsed}s  在播 {len(cur)} 个, updated_at 样本={list(cur.values())[:1]}")
    if elapsed >= 100:
        break

print(f"\n已观察 {int(time.time()-t0)}s\n")

raw = open(LOG, "rb").read()
lines = [dec(x) for x in raw.splitlines()]
start = 0
for i in range(len(lines) - 1, -1, -1):
    if "[启动] 视频号工具" in lines[i]:
        start = i
        break
tail = lines[start:]
print(f"本次启动后 {len(tail)} 行日志")

refreshes = [ln for ln in tail if "重放场次 ID 已刷新" in ln]
per_acc = Counter()
for ln in refreshes:
    for tok in ln.split():
        if tok.startswith("[live:") and tok.endswith("]"):
            per_acc[tok[6:-1]] += 1
            break

rounds = len([ln for ln in tail if "check_live_status 摘要" in ln])
print(f"同期 check_live_status 摘要: {rounds} 条(≈轮次量级)")
print(f"「重放场次 ID 已刷新」: {len(refreshes)} 条")
for acc, n in per_acc.items():
    print(f"   {acc}: {n} 条")

n_live = len([x for x in items() if x.get("is_live")])
print()
ok = True
if len(refreshes) <= max(2, n_live):
    print(f"  PASS  去重生效:{len(refreshes)} 条 <= 在播账号数 {n_live}(每账号约 1 条)")
else:
    print(f"  FAIL  仍在刷屏:{len(refreshes)} 条 > 在播 {n_live} 个")
    ok = False
if any(n > 1 for n in per_acc.values()):
    print(f"  WARN  有账号出现 >1 条(可能是真换场,属正常): {dict(per_acc)}")

errs = [ln for ln in tail if " ERROR " in ln]
print(f"\n  {'PASS' if not errs else 'FAIL'}  ERROR 行: {len(errs)}")
for ln in errs[:3]:
    print("     " + ln[:140])

print("\n" + "=" * 60)
print("RESULT", "PASS" if (ok and not errs) else "FAIL")
sys.exit(0 if (ok and not errs) else 1)
