# -*- coding: utf-8 -*-
"""取证：定位 300800 的真实上下文（前后各 N 行），并统计回复相关行。"""
import io
import sys

src = r"\\Sphgj\sphgj\run.log"


def dec(raw):
    best = None
    for enc in ("utf-8", "gbk"):
        try:
            s = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        bad = s.count("\ufffd")
        if best is None or bad < best[0]:
            best = (bad, s)
        if bad == 0:
            break
    if best is None:
        return raw.decode("gbk", "replace")
    return best[1]


raw = open(src, "rb").read()
lines = [dec(x).rstrip("\r") for x in raw.split(b"\n")]
print("总行数", len(lines))

kw = sys.argv[1] if len(sys.argv) > 1 else "300800"
ctx = int(sys.argv[2]) if len(sys.argv) > 2 else 4
hits = [i for i, l in enumerate(lines) if kw in l]
print(f"命中 {kw}: {len(hits)} 行")
for i in hits[-6:]:
    a, b = max(0, i - ctx), min(len(lines), i + ctx + 1)
    print("=" * 70)
    for j in range(a, b):
        print(("*" if j == i else " ") + lines[j][:400])
