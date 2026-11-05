# -*- coding: utf-8 -*-
"""rev65 投递：先基线校验(母本磁盘 vs git HEAD,CRLF→LF 归一化)，再用 put_file 两步 rename 投递。"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from unc_atomic import put_file, md5_of  # noqa: E402

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [r"backend/server.py", r"backend/post_fetcher.py"]


def norm(b):
    return b.replace(b"\r\n", b"\n")


print("=== 基线校验(母本磁盘 vs git HEAD) ===")
bad = 0
for rel in FILES:
    head = subprocess.check_output(["git", "show", "HEAD:" + rel.replace("\\", "/")],
                                   cwd=SRC)
    disk_path = os.path.join(DST, rel)
    with io.open(disk_path, "rb") as f:
        disk = f.read()
    same = norm(head) == norm(disk)
    # 二次投递:母本上是本脚本上一版刚投的 rev65,带 rev65 标记即认得是自己人
    self_prev = (not same) and b"rev65" in disk
    print("  %s %s  head=%dB disk=%dB %s" % (
        "OK " if (same or self_prev) else "BAD", rel,
        len(norm(head)), len(norm(disk)),
        "(上一版 rev65,允许覆盖)" if self_prev else ""))
    if not (same or self_prev):
        bad += 1
if bad:
    print("BASELINE FAIL: 母本既不是 git HEAD 也不是上一版 rev65,停止投递,先人工核对")
    raise SystemExit(1)
print("BASELINE PASS\n")

print("=== 投递 ===")
for rel in FILES:
    src = os.path.join(SRC, rel)
    dst = os.path.join(DST, rel)
    put_file(src, dst, tag="rev65 " + rel)
    ok = md5_of(dst) == md5_of(src)
    print("  %s %s md5=%s" % ("OK " if ok else "BAD", rel, md5_of(dst)[:12]))
    if not ok:
        bad += 1
print("DELIVER", "OK" if bad == 0 else "FAIL %d" % bad)
raise SystemExit(bad)
