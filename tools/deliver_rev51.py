# -*- coding: utf-8 -*-
"""rev51 投递

backend/api_client.py   写操作按账号灰度切 HTTP + 写失败不重试(防重复评论)
backend/live_fetcher.py 「从磁盘缓存载入已学 API 规格」INFO -> debug（降噪）
一键更新.bat             fetch 显式落 refs/remotes/origin/<branch>，不再依赖 FETCH_HEAD

无前端改动；母本 config.json 无需修改（灰度名单默认为空 = 现状不变）。
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
    "backend/api_client.py",
    "backend/live_fetcher.py",
    "一键更新.bat",
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


print("===== 基线校验(母本磁盘 vs 当前提交 %s) =====" % PREV)
bad = 0
for f in FILES:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-30s 母本无此文件(新建) -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    old = git_raw(f)
    if old is None:
        print("  %-30s 仓库无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-30s 一致 (%d 字节)" % (f, len(cur)))
    else:
        bad += 1
        print("  %-30s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

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
    print("  %-30s %-5s 旧=%s 新=%s  %s"
          % (f, r, before, after, "OK" if ok else "!! 不一致"))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
sys.exit(1 if fail else 0)
