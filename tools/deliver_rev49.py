# -*- coding: utf-8 -*-
"""rev49 投递:直播探测「周期性完整检测」默认关闭(live_probe_full_every=0)

改动: backend/account_manager.py 单文件
  - full_every 支持 0 = 永不周期性落完整检测(原 max(1,..) 会把 0 变成 1)
  - 默认值 5 -> 0
  - 未播日志不再打印 N/0

无前端改动(dist 不动)。母本 config.json 没有该键,代码默认值直接生效。
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

FILES = [
    "backend/account_manager.py",
]


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
        print("  %-42s 母本无此文件(新建) -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    old = git_raw(f)
    if old is None:
        print("  %-42s 仓库无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-42s 一致" % f)
    else:
        bad += 1
        print("  %-42s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

if bad:
    print("\n[中止] 有 %d 个文件母本版与仓库版不一致" % bad)
    sys.exit(1)

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
    print("  %-42s %-5s 旧=%s 新=%s  %s"
          % (f, r, before, after, "OK" if ok else "!! 不一致"))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
