# -*- coding: utf-8 -*-
"""rev59 投递基线校验:母本磁盘 == git HEAD(归一化 CRLF->LF)。

投递前必须做:母本磁盘和 HEAD 不一致 = 有人在母本上直接改过代码,盲覆盖会吞掉。
"""
import hashlib
import os
import subprocess
import sys

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"

FILES = [
    "backend/server.py",
    "backend/restart.py",
    "main.py",
    "frontend/dist/index.html",
]


def norm(b):
    return b.replace(b"\r\n", b"\n") if b else b


def head_of(rel):
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=LOCAL, capture_output=True)
    return r.stdout if r.returncode == 0 else None


bad = 0
for rel in FILES:
    dst = os.path.join(REMOTE, rel.replace("/", os.sep))
    head = head_of(rel)
    disk = None
    if os.path.isfile(dst):
        with open(dst, "rb") as f:
            disk = f.read()
    if head is None:
        print(f"  [新增] {rel}  (HEAD 里没有)")
        continue
    if disk is None:
        print(f"  [!!] {rel}: 母本不存在,但 HEAD 里有 —— 需确认是否被误删")
        bad += 1
        continue
    hn, dn = norm(head), norm(disk)
    if hn == dn:
        print(f"  [ok] {rel}  母本 == HEAD")
    else:
        print(f"  [!!] {rel}  母本 != HEAD  "
              f"head={len(hn)}B/md5={hashlib.md5(hn).hexdigest()[:8]}  "
              f"disk={len(dn)}B/md5={hashlib.md5(dn).hexdigest()[:8]}")
        bad += 1

print()
if bad:
    print(f"BASELINE FAIL ({bad} 个文件与 HEAD 不一致,先查清再投递)")
    sys.exit(1)
print("BASELINE PASS")
