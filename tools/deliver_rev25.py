# -*- coding: utf-8 -*-
"""投递 rev25(自动发作品)到母本 —— 两步 rename 通道版。

包含:后端 5 个文件 + 前端 dist + 建 inbox 目录(按视频号短名)。
"""
import io, json, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of, sweep_junk

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")

BACKEND = [
    (r"backend\publisher.py", "publisher.py"),
    (r"backend\publish_queue.py", "publish_queue.py"),
    (r"backend\storage.py", "storage.py"),
    (r"backend\account_manager.py", "account_manager.py"),
    (r"backend\server.py", "server.py"),
]
DIST = [
    (r"frontend\dist\index.html", "dist/index.html"),
    (r"frontend\dist\assets\index-BMI8inGA.js", "dist/assets/index-BMI8inGA.js"),
    (r"frontend\dist\assets\index-CxbOyzUh.css", "dist/assets/index-CxbOyzUh.css"),
]
FILES = BACKEND + DIST

MARKERS = {
    "storage.py": ["migrate_comments_to_finder", "own_comment_pending", "reset_stale_video_counts"],
    "account_manager.py": ["logout_account", "restore_running_accounts", "_log_finder_id"],
    "server.py": ["/api/publish/probe", "/api/comments/finders", "/api/posts/finders"],
    "publisher.py": ["probe_post_create", "_desc_probe"],
}
# 新版才有的标记:母本不能已经存在(防重复打补丁)
NOT_YET = {
    "storage.py": ["publish_tasks"],
    "account_manager.py": ["human_stopped"],
    "server.py": ["/api/publish/status"],
}

print("== 1. 基线校验 ==")
bad = []
for rel, name in FILES:
    p = os.path.join(UNC, rel)
    if name in MARKERS:
        if not os.path.isfile(p):
            bad.append(f"{name} 不存在"); continue
        txt = io.open(p, encoding="utf-8", errors="ignore").read()
        miss = [m for m in MARKERS[name] if m not in txt]
        already = [m for m in NOT_YET.get(name, []) if m in txt]
        print(f"  {name:20s} {os.path.getsize(p):7d}B 缺基线={miss or '无'} 已含新版={already or '无'}")
        if miss:
            bad.append(f"{name} 缺基线 {miss}")
        if already:
            bad.append(f"{name} 已含新版 {already}(可能已投递过)")
    else:
        exists = os.path.isfile(p)
        print(f"  {name:20s} {'已存在' if exists else '不存在(新增)'}")
        if name == "publish_queue.py" and exists:
            txt = io.open(p, encoding="utf-8", errors="ignore").read()
            if "class PublishQueue" in txt:
                bad.append("publish_queue.py 已存在实现")
if bad:
    raise SystemExit("[ABORT] " + "; ".join(bad))

print("== 2. 备份 ==")
bak = os.path.join(LOCAL, "data", f"backup-rev25-{TS}")
os.makedirs(bak, exist_ok=True)
for rel, name in BACKEND:
    p = os.path.join(UNC, rel)
    if os.path.isfile(p):
        shutil.copyfile(p, os.path.join(bak, "before_" + name))
db = os.path.join(UNC, "data", "sphgj.db")
for suf in ("", "-wal"):
    if os.path.isfile(db + suf):
        shutil.copyfile(db + suf, os.path.join(bak, "sphgj.db" + suf))
print(f"  代码快照 + DB -> {bak}")

print("== 3. 清理历史残留 ==")
n = sweep_junk(os.path.join(UNC, "backend"), os.path.join(UNC, "data", "_unc_trash_rev25"),
               patterns=(".rev2", ".probe", ".diag", ".stg", "__st", ".dead", ".bakrev2", ".__st"),
               log=print)
print(f"  搬走 {n} 个")

print("== 4. 投递 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

print("== 5. 建 inbox 目录(按视频号短名) ==")
inbox = os.path.join(UNC, "data", "publish_inbox")
os.makedirs(inbox, exist_ok=True)
for sub in ("_done", "_failed", "_unknown"):
    os.makedirs(os.path.join(inbox, sub), exist_ok=True)
try:
    cfg = json.load(io.open(os.path.join(UNC, "config.json"), encoding="utf-8"))
    dirs = []
    for a in cfg.get("accounts") or []:
        name = (a.get("name") or "").strip()
        short = name.split(".")[-1].strip() if "." in name else name
        if short:
            dirs.append(short)
    for short in sorted(set(dirs)):
        os.makedirs(os.path.join(inbox, short), exist_ok=True)
        print(f"  inbox/{short}")
except Exception as e:
    print(f"  [WARN] 建目录失败: {e}")

print("== 6. 双侧终校验 ==")
bad2 = []
for rel, name in FILES:
    a, b = md5_of(os.path.join(LOCAL, rel)), md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:32s} {a[:12]}")
    if a != b:
        bad2.append(name)
if bad2:
    raise SystemExit(f"[FAIL] 终校验不一致: {bad2}")

html = io.open(os.path.join(UNC, r"frontend\dist\index.html"), encoding="utf-8").read()
assert "index-BMI8inGA.js" in html and "index-CxbOyzUh.css" in html, "母本 index.html 未指向新 bundle"
print("[OK] 母本 index.html -> index-BMI8inGA.js / index-CxbOyzUh.css")
print(f"[DONE] rev25 投递完成 @ {TS}")
