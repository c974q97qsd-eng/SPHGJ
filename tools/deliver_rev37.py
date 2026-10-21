# -*- coding: utf-8 -*-
"""rev37 投递:登录判定 + 远程串流注入登录态 -> 母本。

基线校验用 CRLF 归一化后比对(仓库存 LF、母本磁盘是 CRLF,直接比会假报不一致)。
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import md5_of, put_file  # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"

FILES = [
    "backend/account_manager.py",
    "backend/remote_view.py",
    "backend/local_window.py",
]


def norm(b):
    return hashlib.md5(b.replace(b"\r\n", b"\n")).hexdigest()


print("=== 1. 基线校验(母本 vs git %s) ===" % PREV)
bad = 0
for f in FILES:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-32s 母本无此文件(新增) -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    try:
        old = subprocess.run(["git", "show", "%s:%s" % (PREV, f)], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except Exception:
        print("  %-32s git 里无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-32s 一致" % f)
    else:
        bad += 1
        print("  %-32s !! 内容不同(母本 %d 字节 / git %d 字节)" % (f, len(cur), len(old)))

print("\n=== 2. 投递 ===")
for f in FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    try:
        r = put_file(src, dst)
    except Exception as e:
        print("  %-32s !! 异常 %s" % (f, e))
        bad += 1
        continue
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    local = md5_of(src)[:8]
    ok = after == local
    if not ok:
        bad += 1
    print("  %-32s %-4s 旧=%s 新=%s 本地=%s %s"
          % (f, r, before, after, local, "OK" if ok else "!!不一致"))

print("\n=== 3. 母本延时重读校验 ===")
import time
time.sleep(1.0)
for f in FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    for i in range(6):
        if md5_of(dst) == md5_of(src):
            print("  %-32s 一致(第 %d 次读)" % (f, i + 1))
            break
        time.sleep(1.0)
    else:
        print("  %-32s !! 重读仍不一致" % f)
        bad += 1

print("\nRESULT:", "ALL OK" if bad == 0 else "有 %d 项失败" % bad)
