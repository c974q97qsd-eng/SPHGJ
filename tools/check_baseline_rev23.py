# -*- coding: utf-8 -*-
"""基线比对:母本磁盘版 vs 本地 git HEAD(确认母本没有丢失/回退,也没有平行热改)。"""
import hashlib, io, os, subprocess

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"
FILES = [r"backend\storage.py", r"backend\post_fetcher.py", r"backend\server.py",
         r"backend\schemas.py", r"backend\account_manager.py", r"backend\login_capture.py"]

def md5b(b):
    return hashlib.md5(b).hexdigest()

def head_bytes(rel):
    return subprocess.check_output(["git", "-C", LOCAL, "show", "HEAD:" + rel.replace("\\", "/")])

for rel in FILES:
    p = os.path.join(UNC, rel)
    if not os.path.isfile(p):
        print(f"{rel:34s} 母本缺失")
        continue
    u = md5b(io.open(p, "rb").read())
    try:
        h = md5b(head_bytes(rel))
    except subprocess.CalledProcessError:
        print(f"{rel:34s} git HEAD 无此文件")
        continue
    print(f"{rel:34s} 母本={u[:12]} HEAD={h[:12]} {'一致' if u == h else '★不一致'}")
