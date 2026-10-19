# -*- coding: utf-8 -*-
"""Step0-C 投递:上传探测版 publisher + server,并放一个测试视频到母本。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
TEST_SRC = r"D:\D抖音下载\复古造型老爹鞋_打造休闲运动风_U_S_POLO_ASSN_官方品牌_春夏上新.mp4"

FILES = [
    (r"backend\publisher.py", "publisher.py"),
    (r"backend\server.py", "server.py"),
]

print("== 投递代码 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 投放测试视频 ==")
d = os.path.join(UNC, "data", "_pubtest")
os.makedirs(d, exist_ok=True)
dst = os.path.join(d, "probe.mp4")
put_file(TEST_SRC, dst, "probe.mp4")

print("== 终校验 ==")
bad = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:14s} {a[:12]} {os.path.getsize(os.path.join(UNC, rel))}B")
    if a != b:
        bad.append(name)
pa, pb = md5_of(TEST_SRC), md5_of(dst)
print(f"  {'OK ' if pa == pb else 'BAD'} {'probe.mp4':14s} {pa[:12]} {os.path.getsize(dst)}B")
if pa != pb:
    bad.append("probe.mp4")
if bad:
    raise SystemExit(f"[FAIL] {bad}")
print(f"[DONE] step0-C 投递完成 @ {TS}")
