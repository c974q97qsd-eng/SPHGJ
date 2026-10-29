# -*- coding: utf-8 -*-
"""rev57 核对第四弹:*_live_http.json 里到底是不是 get_live_info 的响应。"""
import io
import json
import os

PROBE = r"\\Sphgj\sphgj\data\live_probe"
for fn in sorted(os.listdir(PROBE)):
    if "live_http" not in fn:
        continue
    p = os.path.join(PROBE, fn)
    try:
        j = json.load(io.open(p, encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"{fn}: read fail {e}")
        continue
    print("=" * 66)
    print(fn)
    print("  top keys :", sorted(j)[:14])
    print("  errCode  :", j.get("errCode"), " errMsg:", j.get("errMsg"))
    d = j.get("data")
    print("  data type:", type(d).__name__)
    if isinstance(d, dict):
        print("  data keys:", sorted(d))
        st = d.get("liveStats")
        print("  liveStats:", "有 -> %s" % sorted(st)[:20] if isinstance(st, dict) else repr(st))
    elif isinstance(d, (int, type(None))):
        print("  data     :", d)
