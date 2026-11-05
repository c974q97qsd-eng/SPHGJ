# -*- coding: utf-8 -*-
"""rev67 投递：基线校验(母本磁盘 vs git HEAD,CRLF→LF 归一化) + put_file 两步 rename。

母本不构建前端(frontend/src 是 rev57 之前的历史残留),只投后端 + dist。
本次 CSS hash 未变(index-EFSVkcjq.css 与 rev66 相同),只做基线核对不重复投递。
"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from unc_atomic import put_file, md5_of  # noqa: E402

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [
    r"backend\server.py",
    r"backend\remote_view.py",
    r"frontend\dist\index.html",
    r"frontend\dist\assets\index-B5EiYXq2.js",
]
CHECK_ONLY = [
    r"frontend\dist\assets\index-EFSVkcjq.css",
]


def norm(b):
    return b.replace(b"\r\n", b"\n")


print("=== 基线校验(母本磁盘 vs git HEAD) ===")
bad = 0
for rel in FILES + CHECK_ONLY:
    git_rel = rel.replace("\\", "/")
    disk_path = os.path.join(DST, rel)
    try:
        head = subprocess.check_output(["git", "show", "HEAD:" + git_rel],
                                       cwd=SRC, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        head = None                       # 新增文件(vite hash 名每次构建会变)
    if not os.path.exists(disk_path):
        print("  NEW %s (母本无此文件,将新建)" % rel)
        continue
    with io.open(disk_path, "rb") as f:
        disk = f.read()
    if head is None:
        print("  NEW %s (HEAD 无此文件,母本已有 -> 覆盖)" % rel)
        continue
    same = norm(head) == norm(disk)
    print("  %s %s head=%dB disk=%dB" % (
        "OK " if same else "BAD", rel, len(norm(head)), len(norm(disk))))
    if not same:
        bad += 1
if bad:
    print("BASELINE FAIL: 母本与 git HEAD 不一致,停止投递,先人工核对")
    raise SystemExit(1)
print("BASELINE PASS\n")

print("=== 投递 ===")
for rel in FILES:
    src = os.path.join(SRC, rel)
    dst = os.path.join(DST, rel)
    put_file(src, dst, tag="rev67 " + rel)
    ok = md5_of(dst) == md5_of(src)
    print("  %s %s md5=%s" % ("OK " if ok else "BAD", rel, md5_of(dst)[:12]))
    if not ok:
        bad += 1

print("\n=== 母本首页引用核对 ===")
html = io.open(os.path.join(DST, r"frontend\dist\index.html"),
               encoding="utf-8", errors="replace").read()
for name in ("index-B5EiYXq2.js", "index-EFSVkcjq.css"):
    exists = os.path.exists(os.path.join(DST, "frontend", "dist", "assets", name))
    print("  %s %s referenced=%s exists=%s" % (
        "OK " if (name in html and exists) else "BAD", name,
        name in html, exists))
    if not (name in html and exists):
        bad += 1

print("DELIVER", "OK" if bad == 0 else "FAIL %d" % bad)
raise SystemExit(bad)
