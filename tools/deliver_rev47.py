# -*- coding: utf-8 -*-
"""rev47 投递:直播探测频率解耦 + 状态机合并 + DOM 判据

后端: backend/live_fetcher.py, backend/account_manager.py
前端: 无改动(dist 不变)
新增运行期文件: data/live_api_spec.json(首次学到接口时自动生成,不入库)
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"

BACKEND = ["backend/live_fetcher.py", "backend/account_manager.py"]


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
        print("  %-30s 母本无此文件 -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    old = git_raw(f)
    if old is None:
        print("  %-30s 仓库无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-30s 一致" % f)
    else:
        bad += 1
        print("  %-30s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

print("\n===== 投递 =====")
fail = 0
for f in BACKEND:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    r = put_file(src, dst)
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    ok = after == md5_of(src)[:8]
    if not ok:
        fail += 1
    print("  %-30s %-5s 旧=%s 新=%s  %s"
          % (f, r, before, after, "OK" if ok else "!! 不一致"))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
