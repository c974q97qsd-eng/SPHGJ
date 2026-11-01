# -*- coding: utf-8 -*-
"""投递 rev62 到母本(UNC 原子写)。"""
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file   # noqa: E402

FILES = [r"backend\api_client.py",
         r"backend\auto_comment.py",
         r"backend\comment_fetcher.py"]
SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"

for rel in FILES:
    put_file(SRC + "\\" + rel, DST + "\\" + rel, tag="rev62")
    print("  ok  " + rel)

print("\n投递完成 %d/%d" % (len(FILES), len(FILES)))
