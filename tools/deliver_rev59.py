# -*- coding: utf-8 -*-
"""rev59 投递到母本 \\\\Sphgj\\sphgj(192.168.1.27 的 D:\\SPHGJ)。

内容:停机链路 —— 清理改由 FastAPI lifespan shutdown 拥有(rev59)+ 并发重启保护(rev59)。
  母本 run.log 每次都打:
      WARNING graceful_shutdown 异常: Event loop is closed
      RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
  即清理一次都没跑成,随后 os._exit 硬砍 Chromium(孤儿进程 + cookie 可能未落盘)。

投递 3 个后端文件 + 前端 index.html/js;旧 hash 的 js 搬到 data/_unc_trash_rev59
(UNC 上删不掉,只能同卷 rename)。

用法: python tools/deliver_rev59.py
"""
import os
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file          # noqa: E402

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"

FILES = [
    "backend/server.py",
    "backend/restart.py",
    "main.py",
    "frontend/dist/index.html",
    "frontend/dist/assets/index-_ZmJ7kWF.js",
]

# 上一版 index.html 引用的旧产物(搬走,否则留孤儿文件)
OLD_ASSETS = [
    "frontend/dist/assets/index-CrIViLuP.js",
]

print("=" * 66)
ok = 0
failed = []
for rel in FILES:
    src = os.path.join(LOCAL, rel.replace("/", os.sep))
    dst = os.path.join(REMOTE, rel.replace("/", os.sep))
    print(f"\n-> {rel}")
    try:
        put_file(src, dst, tag="rev59")
        ok += 1
    except Exception as e:
        failed.append(rel)
        print(f"   FAIL: {e!r}")

trash = os.path.join(REMOTE, "data", "_unc_trash_rev59")
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
        print(f"\n[trash] {os.path.basename(rel)} -> data/_unc_trash_rev59/")
    except Exception as e:
        print(f"\n[trash] {os.path.basename(rel)} 搬移失败(不影响运行,index.html 已不引用): {e!r}")

print("\n" + "=" * 66)
print(f"投递完成 {ok}/{len(FILES)}" + (f"  失败: {failed}" if failed else ""))
sys.exit(0 if ok == len(FILES) else 1)
