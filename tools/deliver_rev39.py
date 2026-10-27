# -*- coding: utf-8 -*-
"""rev39 投递：一键更新.bat / README.md / tools/gen_rev34_bats.py -> 母本。"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"
FILES = ["一键更新.bat", "README.md", "tools/gen_rev34_bats.py"]


def norm(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_raw(path):
    try:
        p = subprocess.run(["git", "show", "%s:%s" % (PREV, path.replace("\\", "/"))],
                           cwd=ROOT, capture_output=True, check=True)
        return p.stdout
    except Exception:
        return None


print("===== 基线校验(母本磁盘 vs 当前提交) =====")
bad = 0
for f in FILES:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-28s 母本无此文件(新增) -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    old = git_raw(f)
    if old is None:
        print("  %-28s 仓库无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-28s 一致" % f)
    else:
        bad += 1
        print("  %-28s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

print("\n===== 投递 =====")
fail = 0
for f in FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    r = put_file(src, dst)
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    ok = after == md5_of(src)[:8]
    if not ok:
        fail += 1
    print("  %-28s %-6s 旧=%s 新=%s 本地=%s  %s"
          % (f, r, before, after, md5_of(src)[:8], "OK" if ok else "!! 不一致"))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
