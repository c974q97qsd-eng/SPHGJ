# -*- coding: utf-8 -*-
"""rev60 投递基线校验:母本磁盘 == git HEAD(归一化 CRLF->LF)。

投递前必须做:母本磁盘和 HEAD 不一致 = 有人在母本上直接改过代码,盲覆盖会吞掉。
"""
import subprocess
import sys

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"

FILES = [
    "backend/account_manager.py",
    "backend/server.py",
]


def norm(b):
    return b.replace(b"\r\n", b"\n") if b else b


def head_of(rel):
    r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=LOCAL, capture_output=True)
    return r.stdout if r.returncode == 0 else None


bad = 0
for rel in FILES:
    dst = REMOTE + "\\" + rel.replace("/", "\\")
    head = head_of(rel)
    try:
        with open(dst, "rb") as f:
            disk = f.read()
    except Exception as e:
        print(f"  [缺失] {rel}  读取失败: {e}")
        bad += 1
        continue
    if head is None:
        print(f"  [新增] {rel}  (HEAD 里没有,首次投递)")
        continue
    if norm(disk) == norm(head):
        print(f"  ok    {rel}  ({len(disk)} 字节)")
    else:
        bad += 1
        print(f"  DIFF  {rel}  母本 {len(disk)} 字节 / HEAD {len(head)} 字节"
              f"  <- 母本被直接改过,先查清再投")

print()
if bad:
    print(f"BASELINE FAIL: {bad} 个文件不一致,不要投递")
    sys.exit(1)
print("BASELINE PASS:母本 == HEAD,可以投递")
