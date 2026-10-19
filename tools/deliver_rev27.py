# -*- coding: utf-8 -*-
"""rev27 投递:远程同步浏览 + 作品播放/复制标题。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL, UNC = r"D:\SPHGJ", r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
FILES = [
    (r"backend\remote_view.py", "remote_view.py"),
    (r"backend\server.py", "server.py"),
    (r"backend\storage.py", "storage.py"),
    (r"backend\post_fetcher.py", "post_fetcher.py"),
    (r"frontend\dist\index.html", "index.html"),
    (r"frontend\dist\assets\index-CuM3pgct.js", "index-CuM3pgct.js"),
    (r"frontend\dist\assets\index-BsNRQig9.css", "index-BsNRQig9.css"),
]

print("== 基线 ==")
for rel, name in FILES:
    p = os.path.join(UNC, rel)
    print(f"  {name:22s} {'(不存在,将新建)' if not os.path.exists(p) else str(os.path.getsize(p)) + 'B'}")

bak = os.path.join(LOCAL, "data", f"backup-rev27-{TS}")
os.makedirs(bak, exist_ok=True)
for rel, name in FILES:
    src = os.path.join(UNC, rel)
    if os.path.exists(src):
        shutil.copyfile(src, os.path.join(bak, name))
print(f"  备份 -> {bak}")

print("== 投递 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 终校验 ==")
bad = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:22s} {a[:12]} {os.path.getsize(os.path.join(UNC, rel))}B")
    if a != b:
        bad.append(name)
if bad:
    raise SystemExit(f"[FAIL] {bad}")
print(f"[DONE] rev27 投递完成 @ {TS}")
