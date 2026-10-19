# -*- coding: utf-8 -*-
"""Step0 投递:新增 backend/publisher.py(DOM 探测) + server.py 挂诊断接口。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of, sweep_junk

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")

FILES = [
    (r"backend\publisher.py", "publisher.py"),
    (r"backend\server.py", "server.py"),
]
MARKERS = {"server.py": ["/api/posts/finders", "auto_restore_running", "/logout"]}

print("== 1. 基线校验 ==")
bad = []
for rel, name in FILES:
    p = os.path.join(UNC, rel)
    if name in MARKERS:
        txt = io.open(p, encoding="utf-8", errors="ignore").read()
        miss = [m for m in MARKERS[name] if m not in txt]
        print(f"  {name:16s} {os.path.getsize(p):7d}B 缺={miss or '无'}")
        if miss:
            bad.append(f"{name} 缺{miss}")
    else:
        print(f"  {name:16s} 母本{'已存在' if os.path.isfile(p) else '不存在(新增)'}")
if bad:
    raise SystemExit("[ABORT] " + "; ".join(bad))

print("== 2. 备份 ==")
bak = os.path.join(LOCAL, "data", f"backup-pub-step0-{TS}")
os.makedirs(bak, exist_ok=True)
shutil.copyfile(os.path.join(UNC, r"backend\server.py"), os.path.join(bak, "before_server.py"))
print(f"  -> {bak}")

print("== 3. 投递 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 4. 终校验 ==")
bad2 = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:16s} {a[:12]}")
    if a != b:
        bad2.append(name)
if bad2:
    raise SystemExit(f"[FAIL] {bad2}")
print(f"[DONE] step0 投递完成 @ {TS}")
