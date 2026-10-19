# -*- coding: utf-8 -*-
"""rev26 投递:发布成功重命名素材(storage/publish_queue + 前端 dist)。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL, UNC = r"D:\SPHGJ", r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
FILES = [(r"backend\storage.py", "storage.py", "archived_name"),
         (r"backend\publish_queue.py", "publish_queue.py", "rename_after_publish"),
         (r"frontend\dist\index.html", "index.html", "index-"),
         (r"frontend\dist\assets\index-BuHHqge-.js", "index-BuHHqge-.js", "rename_after_publish"),
         (r"frontend\dist\assets\index-CxbOyzUh.css", "index-CxbOyzUh.css", "css")]

print("== 基线 ==")
for rel, name, mark in FILES:
    p = os.path.join(UNC, rel)
    if not os.path.exists(p):
        print(f"  {name:22s} {'(母本不存在,将新建)':>10s}")
        continue
    txt = io.open(p, encoding="utf-8", errors="ignore").read()
    print(f"  {name:22s} {os.path.getsize(p):8d}B 已含新标记={mark in txt}")

bak = os.path.join(LOCAL, "data", f"backup-rev26-{TS}")
os.makedirs(bak, exist_ok=True)
for rel, name, _ in FILES:
    src = os.path.join(UNC, rel)
    if not os.path.exists(src):
        continue
    dst = os.path.join(bak, name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
print(f"  备份 -> {bak}")

print("== 投递 ==")
for rel, name, _ in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 终校验 ==")
bad = []
for rel, name, _ in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:22s} {a[:12]} {os.path.getsize(os.path.join(UNC, rel))}B")
    if a != b:
        bad.append(name)
if bad:
    raise SystemExit(f"[FAIL] {bad}")
print(f"[DONE] rev26 投递完成 @ {TS}")
