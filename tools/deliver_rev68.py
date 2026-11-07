# -*- coding: utf-8 -*-
"""rev68 投递：基线校验(母本磁盘 vs git HEAD,CRLF→LF 归一化) + put_file 两步 rename。

本次只改后端两个文件；前端无改动，不投 dist。
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
    r"backend\live_fetcher.py",
    r"backend\account_manager.py",
]


def norm(b):
    return b.replace(b"\r\n", b"\n")


print("=== 基线校验(母本磁盘 vs git HEAD) ===")
bad = 0
for rel in FILES:
    git_rel = rel.replace("\\", "/")
    disk_path = os.path.join(DST, rel)
    head = subprocess.check_output(["git", "show", "HEAD:" + git_rel],
                                   cwd=SRC, stderr=subprocess.DEVNULL)
    with io.open(disk_path, "rb") as f:
        disk = f.read()
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
    put_file(src, dst, tag="rev68 " + rel)
    ok = md5_of(dst) == md5_of(src)
    print("  %s %s md5=%s" % ("OK " if ok else "BAD", rel, md5_of(dst)[:12]))
    if not ok:
        bad += 1

print("DELIVER", "OK" if bad == 0 else "FAIL %d" % bad)
raise SystemExit(bad)
