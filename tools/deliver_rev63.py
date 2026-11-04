# -*- coding: utf-8 -*-
"""rev63 投递:后端 2 个 py + 前端 dist(index.html + 新 js + css)。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file  # noqa: E402

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"

FILES = [
    r"backend\publish_queue.py",
    r"backend\server.py",
    r"frontend\dist\index.html",
    r"frontend\dist\assets\index-DpCFPwBr.js",
    r"frontend\dist\assets\index-EFSVkcjq.css",
]

results = []
for rel in FILES:
    src = os.path.join(SRC, rel)
    dst = os.path.join(DST, rel)
    if not os.path.isfile(src):
        print("[MISS] 本地缺文件:", rel)
        results.append((rel, "miss"))
        continue
    if not os.path.isdir(os.path.dirname(dst)):
        print("[MKDIR] 母本补目录:", os.path.dirname(rel))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        r = put_file(src, dst, tag=rel)
    except Exception as e:
        print("[FAIL] %s: %s" % (rel, e))
        r = "fail"
    results.append((rel, r))

print("\n==== 投递结果 ====")
for rel, r in results:
    print("  %-46s %s" % (rel, r))
bad = [r for _, r in results if r not in ("ok", "skip")]
print("RESULT", "ALL OK" if not bad else "有 %d 个失败" % len(bad))
raise SystemExit(0 if not bad else 1)
