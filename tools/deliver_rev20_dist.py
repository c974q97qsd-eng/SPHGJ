# -*- coding: utf-8 -*-
"""rev20: 投递前端 dist 到 UNC(index.html 已存在 -> 同卷 rename 覆盖)。"""
import io
import os
import shutil

UNC = r"\\Sphgj\sphgj"
LOCAL_DIST = r"D:\SPHGJ\frontend\dist"
UNC_DIST = UNC + r"\frontend\dist"
TAG = "rev20"

# 1) 新资源文件直拷
src_assets = os.path.join(LOCAL_DIST, "assets")
for name in os.listdir(src_assets):
    src = os.path.join(src_assets, name)
    dst = os.path.join(UNC_DIST, "assets", name)
    if os.path.exists(dst):
        print("[SKIP] 已存在:", name)
        continue
    shutil.copy2(src, dst)
    with io.open(src, "rb") as f1, io.open(dst, "rb") as f2:
        assert f1.read() == f2.read(), name
    print("[COPY]", name)

# 2) index.html 同卷 rename 覆盖(宿主 serve 时会有瞬时锁,重试穿透)
import time

with io.open(os.path.join(LOCAL_DIST, "index.html"), "rb") as f:
    html = f.read()
target = os.path.join(UNC_DIST, "index.html")
stage = target + f".stage_{TAG}"
last_err = None
for i in range(15):
    try:
        with io.open(stage, "wb") as f:
            f.write(html)
        os.replace(stage, target)
        with io.open(target, "rb") as f:
            assert f.read() == html
        print(f"[DELIVER] index.html 已覆盖并校验(第 {i+1} 次)")
        break
    except Exception as e:
        last_err = e
        try:
            os.remove(stage)
        except Exception:
            pass
        time.sleep(1)
else:
    raise SystemExit(f"index.html 覆盖失败: {type(last_err).__name__}: {last_err}")

print("DIST DELIVER DONE")
