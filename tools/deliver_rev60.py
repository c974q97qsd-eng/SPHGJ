# -*- coding: utf-8 -*-
"""投递 rev60 到母本(UNC 原子写)。"""
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file   # noqa: E402

FILES = [
    r"backend\account_manager.py",
    r"backend\server.py",
]

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"

ok = 0
for rel in FILES:
    put_file(SRC + "\\" + rel, DST + "\\" + rel, tag="rev60")
    print(f"  ok  {rel}")
    ok += 1

print(f"\n投递完成 {ok}/{len(FILES)}")
