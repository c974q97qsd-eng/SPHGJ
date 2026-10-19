# -*- coding: utf-8 -*-
"""把之前被错误归档到 _failed/ 的测试素材移回 inbox/优选仓/。"""
import os, sys

UNC = r"\\Sphgj\sphgj"
inbox = os.path.join(UNC, r"data\publish_inbox")
failed = os.path.join(inbox, "_failed")
target = os.path.join(inbox, "优选仓")
os.makedirs(target, exist_ok=True)
n = 0
if os.path.isdir(failed):
    for fn in os.listdir(failed):
        if not fn.lower().endswith((".mp4", ".m4v", ".mov")):
            continue
        src = os.path.join(failed, fn)
        # 去掉时间戳前缀 xxx_ 还原文件名
        base = fn.split("_", 1)[1] if fn[:8].isdigit() else fn
        dst = os.path.join(target, base)
        try:
            os.rename(src, dst)   # UNC 同卷纯 rename,允许
            print("restored:", base)
            n += 1
        except Exception as e:
            print("[WARN]", fn, e)
print(f"恢复 {n} 个文件")
print("inbox/优选仓:", sorted(os.listdir(target)))
