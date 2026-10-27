# -*- coding: utf-8 -*-
"""rev38 投递:登录时间同步(后端) + 发布记录分页(前端) -> 母本。

基线校验用 CRLF 归一化后比对(仓库存 LF、母本磁盘是 CRLF,直接比会假报不一致)。
dist 里的旧哈希文件在 UNC 上删不掉,只能 rename 搬进 data/_unc_trash_rev23/。
"""
import hashlib
import os
import subprocess
import sys
import time

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import md5_of, put_file  # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"
TRASH = os.path.join(DST, "data", "_unc_trash_rev23")

# 后端 + 前端源码(运行时只用 dist,源码同步是为了母本与仓库保持一致)
FILES = [
    "backend/account_manager.py",
    "frontend/src/pages/AccountsPage.tsx",
    "frontend/src/components/publish/TaskTable.tsx",
    "frontend/src/components/common/Pager.tsx",
]

# dist 全量(local 里有的都投;母本多出来的旧哈希文件搬进 trash)
DIST_FILES = [
    "frontend/dist/index.html",
    "frontend/dist/assets/index-B6e9Y6gM.js",
    "frontend/dist/assets/index-Ch5lL9oH.css",
]

KEEP = {"index.html", "index-B6e9Y6gM.js", "index-Ch5lL9oH.css"}


def norm(b):
    return hashlib.md5(b.replace(b"\r\n", b"\n")).hexdigest()


bad = 0

print("=== 1. 基线校验(母本 vs git %s) ===" % PREV)
for f in FILES + DIST_FILES:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-46s 母本无此文件(新增) -> 跳过基线" % f)
        continue
    cur = open(dst, "rb").read()
    try:
        old = subprocess.run(["git", "show", "%s:%s" % (PREV, f)], cwd=ROOT,
                             capture_output=True, check=True).stdout
    except Exception:
        print("  %-46s git 里无此文件 -> 跳过基线" % f)
        continue
    if md5_of(dst) == md5_of(os.path.join(ROOT, f.replace("/", os.sep))):
        print("  %-46s 与本地新版相同(已投过)" % f)
    elif norm(cur) == norm(old):
        print("  %-46s 与 git %s 一致(干净基线)" % (f, PREV))
    else:
        # 要改的文件本来就与上一版不同,这里只如实标注,不阻断
        print("  %-46s 与上一版不同(本次要改它 / 或母本被手改过,请对照)" % f)

print("\n=== 2. 投递 ===")
for f in FILES + DIST_FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    if not os.path.exists(src):
        print("  %-46s !! 本地不存在" % f)
        bad += 1
        continue
    dst = os.path.join(DST, f.replace("/", os.sep))
    parent = os.path.dirname(dst)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    try:
        r = put_file(src, dst)
    except Exception as e:
        print("  %-46s !! 异常 %s" % (f, e))
        bad += 1
        continue
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    local = md5_of(src)[:8]
    ok = after == local
    if not ok:
        bad += 1
    print("  %-46s %-4s 旧=%s 新=%s 本地=%s %s"
          % (f, r, before, after, local, "OK" if ok else "!!不一致"))

print("\n=== 3. dist 旧哈希文件搬进 trash ===")
assets = os.path.join(DST, "frontend", "dist", "assets")
os.makedirs(TRASH, exist_ok=True)
moved = []
if os.path.isdir(assets):
    for fn in sorted(os.listdir(assets)):
        if fn in KEEP:
            continue
        try:
            os.rename(os.path.join(assets, fn), os.path.join(TRASH, "rev38_" + fn))
            moved.append(fn)
        except Exception as e:
            print("  %-46s !! 搬移失败 %s" % (fn, e))
            bad += 1
print("  搬走 %d 个: %s" % (len(moved), moved or "无"))
print("  dist/assets 现在: %s" % sorted(os.listdir(assets)) if os.path.isdir(assets) else "  目录不存在")

print("\n=== 4. 延时重读校验 ===")
time.sleep(1.0)
for f in FILES + DIST_FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    for i in range(6):
        if md5_of(dst) == md5_of(src):
            print("  %-46s 一致(第 %d 次读)" % (f, i + 1))
            break
        time.sleep(1.0)
    else:
        print("  %-46s !! 重读仍不一致" % f)
        bad += 1

print("\nRESULT:", "ALL OK" if bad == 0 else "有 %d 项失败" % bad)
sys.exit(0 if bad == 0 else 1)
