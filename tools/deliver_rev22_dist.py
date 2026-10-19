# -*- coding: utf-8 -*-
"""投递 rev22 前端 dist 到母本。

策略(rev20 已验证):
  - assets 下新 hash 文件直接写入(UNC 允许新建文件);
  - index.html 走「同卷暂存 -> os.replace」覆盖(已存在文件唯一通道);
  - 宿主正在 serve 这些文件,rename 可能有瞬时锁 -> 多次重试 + 延时重读校验。
"""
import io
import os
import time
import uuid

LOCAL_DIST = r"D:\SPHGJ\frontend\dist"
UNC_DIST = r"\\Sphgj\sphgj\frontend\dist"

# 1) 先枚举差异,顺便清理母本侧过期的旧 hash 资源(仅限 index-*.js/css)
local_assets = sorted(os.listdir(os.path.join(LOCAL_DIST, "assets")))
print("本地 assets:", local_assets)

unc_assets = sorted(os.listdir(os.path.join(UNC_DIST, "assets")))
print("母本 assets:", unc_assets)

# 2) 拷新资源(直接写,新文件可写)
for name in local_assets:
    src = os.path.join(LOCAL_DIST, "assets", name)
    dst = os.path.join(UNC_DIST, "assets", name)
    with io.open(src, "rb") as f:
        data = f.read()
    if os.path.exists(dst):
        with io.open(dst, "rb") as f:
            if f.read() == data:
                print(f"[SKIP] assets/{name} 已存在且一致")
                continue
    for attempt in range(30):
        stage = dst + f".rev22_{uuid.uuid4().hex[:8]}"
        try:
            with io.open(stage, "wb") as f:
                f.write(data)
            os.replace(stage, dst)
            break
        except PermissionError:
            try:
                os.remove(stage)
            except Exception:
                pass
            time.sleep(1.0)
    else:
        raise SystemExit(f"[FAIL] assets/{name} 投递失败")
    with io.open(dst, "rb") as f:
        assert f.read() == data, f"{name} 落盘校验失败"
    print(f"[DELIVER] assets/{name} ok ({len(data)}B)")

# 3) index.html 覆盖(宿主正在 serve,重试穿透瞬时锁)
with io.open(os.path.join(LOCAL_DIST, "index.html"), "rb") as f:
    html = f.read()
target = os.path.join(UNC_DIST, "index.html")
for attempt in range(40):
    stage = target + f".rev22_{uuid.uuid4().hex[:8]}"
    try:
        with io.open(stage, "wb") as f:
            f.write(html)
        os.replace(stage, target)
        break
    except PermissionError:
        try:
            os.remove(stage)
        except Exception:
            pass
        time.sleep(1.5)
else:
    raise SystemExit("[FAIL] index.html 投递失败")

for _ in range(6):
    time.sleep(1.0)
    with io.open(target, "rb") as f:
        if f.read() == html:
            print("[DELIVER] index.html ok(已校验)")
            break
else:
    raise SystemExit("[FAIL] index.html 落盘校验失败")

# 4) 校验母本 serve 的入口确实指向新 bundle
with io.open(target, encoding="utf-8") as f:
    content = f.read()
new_js = [a for a in local_assets if a.endswith(".js")][0]
assert new_js in content, f"index.html 未引用新 bundle {new_js}"
print(f"[OK] 母本 index.html 已指向 {new_js}")

# 5) 顺带报告母本里多余的旧 hash 资源(不删,只报告,避免误删正在被引用的文件)
stale = [a for a in unc_assets if a not in local_assets]
if stale:
    print("[INFO] 母本仍残留旧资源(无害,可留待手工清理):", stale)
