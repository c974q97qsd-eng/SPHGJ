# -*- coding: utf-8 -*-
"""rev64 投递：本地两个源码 -> 母本 UNC（只用 unc_atomic.put_file 两步 rename）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from unc_atomic import put_file, md5_of  # noqa: E402

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [r"backend\server.py", r"backend\post_fetcher.py"]

bad = 0
for rel in FILES:
    src = os.path.join(SRC, rel)
    dst = os.path.join(DST, rel)
    r = put_file(src, dst, tag="rev64 " + rel)
    with open(src, "rb") as f:
        want = md5_of(src)
    got = md5_of(dst)
    ok = got == want
    print("  %s %s md5=%s" % ("OK " if ok else "BAD", rel, got[:12]))
    if not ok:
        bad += 1
print("DELIVER", "OK" if bad == 0 else "FAIL %d" % bad)
raise SystemExit(bad)
