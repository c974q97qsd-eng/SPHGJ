# -*- coding: utf-8 -*-
"""rev36 投递:一键更新.bat / README.md / .gitignore / 生成器 -> 母本,
并在母本放置 .no_git_update 标记(决定 A:母本代码走投递,不走 git)。"""
import hashlib
import io
import os
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import md5_of, put_file  # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"

FILES = [
    "一键更新.bat",
    "README.md",
    ".gitignore",
    "tools/gen_rev34_bats.py",
]


def norm(b):
    return hashlib.md5(b.replace(b"\r\n", b"\n")).hexdigest()


print("=== 1. 投递 ===")
bad = 0
for f in FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    try:
        r = put_file(src, dst)
    except Exception as e:
        print("  %-26s !! 投递异常 %s" % (f, e))
        bad += 1
        continue
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    local = md5_of(src)[:8]
    ok = after == local
    if not ok:
        bad += 1
    print("  %-26s %-5s 旧=%s 新=%s 本地=%s %s"
          % (f, r, before, after, local, "OK" if ok else "!!不一致"))

print("\n=== 2. 母本放置运行主机标记 ===")
marker = os.path.join(DST, ".no_git_update")
if os.path.exists(marker):
    print("  已存在,跳过:", md5_of(marker)[:8])
else:
    try:
        with open(marker, "wb") as fp:
            fp.write(b"# SPHGJ running host: code is delivered from dev machine, not git\n")
        print("  已创建:", os.path.exists(marker), os.path.getsize(marker), "字节")
    except Exception as e:
        print("  !! 创建失败:", e)
        bad += 1

print("\n=== 3. 母本复核 ===")
for f in FILES + [".no_git_update"]:
    dst = os.path.join(DST, f.replace("/", os.sep))
    print("  %-26s %s" % (f, "存在" if os.path.exists(dst) else "缺失"))

print("\nRESULT:", "ALL OK" if bad == 0 else "有 %d 项失败" % bad)
