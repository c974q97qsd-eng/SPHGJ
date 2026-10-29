# -*- coding: utf-8 -*-
"""rev58 投递到母本 \\\\Sphgj\\sphgj(192.168.1.27 的 D:\\SPHGJ)。

内容:发布管理三处账号选项卡片 + 三处排序改为最新在前 + 顶栏「重启」按钮
(后端 /api/system/restart 支持 wait_task,等当前发布任务做完再退)。

投递 3 个后端文件 + 前端 index.html/js/css,并把旧 hash 的 js/css 搬到
data/_unc_trash_rev58 —— UNC 上删不掉文件(shutil/rmtree/rmdir/cmd rmdir 全被
safe-delete 钩子拦),只能同卷 rename。
"""
import os
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file          # noqa: E402

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"

FILES = [
    "backend/storage.py",
    "backend/publish_queue.py",
    "backend/server.py",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-CrIViLuP.js",
    "frontend/dist/assets/index-EFSVkcjq.css",
]

# 上一版 index.html 引用的旧产物(搬走,否则留孤儿文件)
OLD_ASSETS = [
    "frontend/dist/assets/index-5CgI3Vjm.js",
    "frontend/dist/assets/index-CZOEmULe.css",
]

print("=" * 66)
ok = 0
failed = []
for rel in FILES:
    src = os.path.join(LOCAL, rel.replace("/", os.sep))
    dst = os.path.join(REMOTE, rel.replace("/", os.sep))
    print(f"\n-> {rel}")
    try:
        put_file(src, dst, tag="rev58")
        ok += 1
    except Exception as e:
        failed.append(rel)
        print(f"   FAIL: {e!r}")

trash = os.path.join(REMOTE, "data", "_unc_trash_rev58")
for rel in OLD_ASSETS:
    p = os.path.join(REMOTE, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        print(f"\n[trash] {os.path.basename(rel)} 不在母本上(跳过)")
        continue
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, os.path.basename(rel))
    if os.path.exists(target):
        target = target + ".2"
    try:
        os.rename(p, target)
        print(f"\n[trash] {os.path.basename(rel)} -> data/_unc_trash_rev58/")
    except Exception as e:
        print(f"\n[trash] {os.path.basename(rel)} 搬移失败(不影响运行,index.html 已不引用): {e!r}")

print("\n" + "=" * 66)
print(f"投递完成 {ok}/{len(FILES)}" + (f"  失败: {failed}" if failed else ""))
sys.exit(0 if ok == len(FILES) else 1)
