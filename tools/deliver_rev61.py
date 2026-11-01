# -*- coding: utf-8 -*-
"""投递 rev61 到母本(UNC 原子写)。"""
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file   # noqa: E402

FILES = [
    r"backend\api_client.py",
    r"backend\server.py",
    r"backend\auto_reply.py",
    r"backend\auto_comment.py",
    r"backend\storage.py",
]

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"

ok = 0
for rel in FILES:
    put_file(SRC + "\\" + rel, DST + "\\" + rel, tag="rev61")
    print("  ok  " + rel)
    ok += 1

print("\n投递完成 %d/%d" % (ok, len(FILES)))
