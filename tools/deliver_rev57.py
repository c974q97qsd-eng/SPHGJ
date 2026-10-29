# -*- coding: utf-8 -*-
"""rev57 投递到母本 \\\\Sphgj\\sphgj(192.168.1.27 的 D:\\SPHGJ)。

为什么叫 rev57 不叫 rev53:仓库里 rev53 已被 e7172b7
「修 _latest_account 兜底分支引用未赋值属性」占用,rev50~rev56d 全部用过。
本次是最新的,顺延到 rev57。

投递 4 个文件 + 把前端旧 hash 的 js 搬到 data/_unc_trash_rev57。
UNC 上删不掉文件(shutil/rmtree/rmdir/cmd rmdir 全被 safe-delete 钩子拦),只能同卷 rename。
"""
import os
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file          # noqa: E402

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"

FILES = [
    "backend/live_fetcher.py",
    "backend/account_manager.py",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-5CgI3Vjm.js",
]

print("=" * 66)
ok = 0
failed = []
for rel in FILES:
    src = os.path.join(LOCAL, rel.replace("/", os.sep))
    dst = os.path.join(REMOTE, rel.replace("/", os.sep))
    print(f"\n-> {rel}")
    try:
        put_file(src, dst, tag="rev57")
        ok += 1
    except Exception as e:
        failed.append(rel)
        print(f"   FAIL: {e!r}")

# 旧 hash 的 js:母本目录里搬走,否则留孤儿文件(无害但脏)
old_js = os.path.join(REMOTE, "frontend", "dist", "assets", "index-1TbppCcc.js")
trash = os.path.join(REMOTE, "data", "_unc_trash_rev57")
if os.path.isfile(old_js):
    os.makedirs(trash, exist_ok=True)
    target = os.path.join(trash, "index-1TbppCcc.js")
    if os.path.exists(target):
        target = target + ".2"
    try:
        os.rename(old_js, target)
        print("\n[trash] index-1TbppCcc.js -> data/_unc_trash_rev57/")
    except Exception as e:
        print(f"\n[trash] 搬移失败(不影响运行,index.html 已不引用它): {e!r}")
else:
    print("\n[trash] 旧 js 不在母本上(跳过)")

print("\n" + "=" * 66)
print(f"投递完成 {ok}/{len(FILES)}" + (f"  失败: {failed}" if failed else ""))
sys.exit(0 if ok == len(FILES) else 1)
