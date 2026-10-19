# -*- coding: utf-8 -*-
"""rev26b 投递:publisher.py + publish_queue.py(发布后回填 object_id)。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL, UNC = r"D:\SPHGJ", r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
FILES = [(r"backend\publisher.py", "publisher.py"),
         (r"backend\publish_queue.py", "publish_queue.py")]

bak = os.path.join(LOCAL, "data", f"backup-rev26b-{TS}")
os.makedirs(bak, exist_ok=True)
for rel, name in FILES:
    shutil.copyfile(os.path.join(UNC, rel), os.path.join(bak, name))
print(f"备份 -> {bak}")

for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

bad = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:18s} {a[:12]}")
    if a != b:
        bad.append(name)
if bad:
    raise SystemExit(f"[FAIL] {bad}")
print(f"[DONE] rev26b @ {TS}")
