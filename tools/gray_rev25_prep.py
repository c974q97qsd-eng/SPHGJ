# -*- coding: utf-8 -*-
"""rev25 灰度准备:把测试视频放进 inbox/优选仓/,并加一条测试标题。"""
import os, shutil, sys

UNC = r"\\Sphgj\sphgj"
src = os.path.join(UNC, r"data\_pubtest\probe.mp4")
dst_dir = os.path.join(UNC, r"data\publish_inbox\优选仓")
os.makedirs(dst_dir, exist_ok=True)
dst = os.path.join(dst_dir, "测试素材_老爹鞋.mp4")
if os.path.isfile(src):
    shutil.copyfile(src, dst)
    print("copied ->", dst, os.path.getsize(dst), "B")
else:
    print("[ERR] 源视频不存在:", src)
    sys.exit(1)
print("inbox 目录内容:")
for fn in sorted(os.listdir(dst_dir)):
    print("  ", fn)
