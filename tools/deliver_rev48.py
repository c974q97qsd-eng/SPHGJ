# -*- coding: utf-8 -*-
"""rev48 投递:直播探测改纯 HTTP(httpx / urllib),不再为探测启浏览器

后端: backend/live_probe_http.py(新增), backend/account_manager.py
依赖: requirements.txt / requirements-lock.txt(加 httpx;母本未装则自动走 urllib)
前端: dist 重建(账号卡片显示"探测直连/需重新扫码")
清理: 旧 dist 产物 index-qS9EdG3X.js 搬进 data/_unc_trash_rev23/
"""
import hashlib
import os
import subprocess
import sys
import time

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"

FILES = [
    "backend/live_probe_http.py",     # 新增(母本无此文件)
    "backend/account_manager.py",
    "requirements.txt",
    "requirements-lock.txt",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-1TbppCcc.js",   # 新产物
]
STALE = ["frontend/dist/assets/index-qS9EdG3X.js"]   # 上一版产物,搬回收站


def norm(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_raw(path):
    try:
        p = subprocess.run(["git", "show", "%s:%s" % (PREV, path.replace("\\", "/"))],
                           cwd=ROOT, capture_output=True, check=True)
        return p.stdout
    except Exception:
        return None


print("===== 基线校验(母本磁盘 vs 当前提交;新增文件跳过) =====")
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
    # 必须归一化 CRLF:仓库存 LF,母本磁盘是 CRLF,直接比 md5 会假报"母本被改过"
    if norm(cur) == norm(old):
        print("  %-42s 一致" % f)
    else:
        bad += 1
        print("  %-42s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

if bad:
    print("\n[中止] 有 %d 个文件母本版与仓库版不一致,先确认母本有没有被手改过" % bad)
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

print("\n===== 清理旧前端产物(搬回收站,UNC 上删不掉) =====")
trash = os.path.join(DST, "data", "_unc_trash_rev23")
os.makedirs(trash, exist_ok=True)
for f in STALE:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-42s 已不存在" % f)
        continue
    try:
        name = "old_dist_%d_%s" % (int(time.time()), os.path.basename(f))
        os.rename(dst, os.path.join(trash, name))
        print("  %-42s 已搬走" % f)
    except Exception as e:
        print("  %-42s 搬走失败: %r" % (f, e))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
