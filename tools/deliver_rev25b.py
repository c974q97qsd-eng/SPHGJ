# -*- coding: utf-8 -*-
"""rev25 增量投递:演练模式(publish_queue.py + server.py)。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL, UNC = r"D:\SPHGJ", r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
FILES = [(r"backend\publish_queue.py", "publish_queue.py"),
         (r"backend\server.py", "server.py")]

print("== 基线 ==")
for rel, name in FILES:
    p = os.path.join(UNC, rel)
    txt = io.open(p, encoding="utf-8", errors="ignore").read()
    print(f"  {name:18s} {os.path.getsize(p):7d}B 含新版标记={'/api/publish/status' in txt or 'class PublishQueue' in txt}")

bak = os.path.join(LOCAL, "data", f"backup-rev25b-{TS}")
os.makedirs(bak, exist_ok=True)
for rel, name in FILES:
    shutil.copyfile(os.path.join(UNC, rel), os.path.join(bak, "before_" + name))
print(f"  备份 -> {bak}")

print("== 投递 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 终校验 ==")
bad = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:18s} {a[:12]}")
    if a != b:
        bad.append(name)
if bad:
    raise SystemExit(f"[FAIL] {bad}")
print(f"[DONE] @ {TS}")
