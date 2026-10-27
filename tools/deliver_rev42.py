# -*- coding: utf-8 -*-
"""rev42 投递:标题池引号保留(其余规则不变)

后端: backend/title_clean.py
前端: frontend/dist/index.html + assets/index-qS9EdG3X.js (css 哈希没变,不用重投)
清理: 旧 js(index-zE7R6T3K.js) 搬回收站
"""
import hashlib
import os
import subprocess
import sys
import uuid

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"

BACKEND = ["backend/title_clean.py"]
DIST = ["frontend/dist/index.html", "frontend/dist/assets/index-qS9EdG3X.js"]
STALE = ["frontend/dist/assets/index-zE7R6T3K.js"]
TRASH = os.path.join(DST, "data", "_unc_trash_rev23")


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
for f in BACKEND:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-28s 母本无此文件 -> 跳过" % f)
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
for f in BACKEND + DIST:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    r = put_file(src, dst)
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    ok = after == md5_of(src)[:8]
    if not ok:
        fail += 1
    print("  %-42s %-5s 旧=%s 新=%s  %s"
          % (f, r, before, after, "OK" if ok else "!! 不一致"))

print("\n===== 清理母本旧产物 =====")
for f in STALE:
    p = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(p):
        print("  %-42s 不存在,跳过" % f)
        continue
    try:
        os.rename(p, os.path.join(TRASH, os.path.basename(p) + "." + uuid.uuid4().hex[:4]))
        print("  %-42s -> 回收站" % f)
    except Exception as e:
        print("  %-42s 搬运失败: %s" % (f, e))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
