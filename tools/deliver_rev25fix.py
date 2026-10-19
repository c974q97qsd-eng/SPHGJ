# -*- coding: utf-8 -*-
"""rev25-fix: 只投 publisher.py 并重启。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of

LOCAL, UNC = r"D:\SPHGJ", r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")
rel, name = r"backend\publisher.py", "publisher.py"
bak = os.path.join(LOCAL, "data", f"backup-rev25fix-{TS}")
os.makedirs(bak, exist_ok=True)
shutil.copyfile(os.path.join(UNC, rel), os.path.join(bak, "before_" + name))
put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)
a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
print(f"{'OK' if a == b else 'BAD'} {name} {a[:12]}")
sys.exit(0 if a == b else 1)
