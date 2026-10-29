# -*- coding: utf-8 -*-
"""看 rev60 那次重启前后的完整日志块。"""
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec     # noqa: E402

with open(r"\\Sphgj\sphgj\run.log", "rb") as f:
    lines = [dec(x) for x in f.read().splitlines()]

# 找 02:36:5x 那一段
sel = [x for x in lines if x[:5] in ("02:36:5", "02:37:0", "02:37:1")]
print(f"02:36:5x~02:37:1x 共 {len(sel)} 行\n")
for x in sel:
    # 只看关键行,跳过浏览器噪音
    if any(k in x for k in ("pid=", "err]")):
        continue
    print(" ", x[:170])
