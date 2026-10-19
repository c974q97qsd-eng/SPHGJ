# -*- coding: utf-8 -*-
"""投递 rev24(评论归属改视频号 finder_id)到母本 —— 两步 rename 通道版。"""
import io, os, sys, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of, sweep_junk

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"
TS = datetime.now().strftime("%Y%m%d-%H%M%S")

FILES = [
    (r"backend\storage.py", "storage.py"),
    (r"backend\comment_fetcher.py", "comment_fetcher.py"),
    (r"backend\auto_reply.py", "auto_reply.py"),
    (r"backend\auto_delete.py", "auto_delete.py"),
    (r"backend\auto_comment.py", "auto_comment.py"),
    (r"backend\account_manager.py", "account_manager.py"),
    (r"backend\server.py", "server.py"),
    (r"backend\schemas.py", "schemas.py"),
    (r"frontend\dist\index.html", "dist/index.html"),
    (r"frontend\dist\assets\index-DSinavzX.js", "dist/assets/index-DSinavzX.js"),
    (r"frontend\dist\assets\index-vuWnffvU.css", "dist/assets/index-vuWnffvU.css"),
]

# ---------- 1) 基线校验(母本必须是 rev23 之后的状态,不能把回退版当基线) ----------
MARKERS = {
    "storage.py": ["migrate_posts_to_finder", "own_comment_pending", "reset_stale_video_counts"],
    "comment_fetcher.py": ["reconcile_own_comments", "FLUSH_THRESHOLD"],
    "auto_reply.py": ["mark_own_comment_pending", "_parse_new_comment_id"],
    "auto_delete.py": ["log_delete", "is_deleted"],
    "auto_comment.py": ["is_auto_commented", "pin_comment"],
    "account_manager.py": ["logout_account", "restore_running_accounts", "_log_finder_id"],
    "server.py": ["/api/posts/finders", "auto_restore_running", "/logout"],
    "schemas.py": ["PostsBatchBody", "PostOpItem"],
}
print("== 1. 基线校验 ==")
bad = []
for rel, name in FILES[:8]:
    p = os.path.join(UNC, rel)
    if not os.path.isfile(p):
        bad.append(name + " 不存在"); continue
    txt = io.open(p, encoding="utf-8", errors="ignore").read()
    miss = [m for m in MARKERS[name] if m not in txt]
    print(f"  {name:20s} {os.path.getsize(p):7d}B md5={md5_of(p)[:12]} 缺={miss or '无'}")
    if miss:
        bad.append(f"{name} 缺{miss}")
if bad:
    raise SystemExit("[ABORT] 母本磁盘版不是预期基线: " + "; ".join(bad))

# ---------- 2) 备份(母本代码 + DB) ----------
print("== 2. 备份 ==")
bak_unc = os.path.join(UNC, "data", f"backup-rev24-{TS}")
bak_local = os.path.join(LOCAL, "data", f"backup-rev24-{TS}")
os.makedirs(bak_local, exist_ok=True)
try:
    os.makedirs(bak_unc, exist_ok=True)
except Exception as e:
    print(f"  [WARN] 母本备份目录: {e}")
for rel, name in FILES[:8]:
    shutil.copyfile(os.path.join(UNC, rel), os.path.join(bak_local, "before_" + name))
print(f"  代码快照 -> {bak_local}")
db = os.path.join(UNC, "data", "sphgj.db")
for suffix in ("", "-wal"):
    src = db + suffix
    if os.path.isfile(src):
        dst = os.path.join(bak_local, "sphgj.db" + suffix)
        shutil.copyfile(src, dst)
        print(f"  DB{suffix or ''} 备份 -> {dst} ({os.path.getsize(dst)}B)")

# ---------- 3) 清理后端历史残留 ----------
print("== 3. 清理残留 ==")
n = sweep_junk(os.path.join(UNC, "backend"), os.path.join(UNC, "data", "_unc_trash_rev24"),
               patterns=(".rev2", ".probe", ".diag", ".stg", "__st", ".dead", ".bakrev2", ".__st"),
               log=print)
print(f"  搬走 {n} 个历史暂存/探针文件")

# ---------- 4) 投递 ----------
print("== 4. 投递 ==")
for rel, name in FILES:
    put_file(os.path.join(LOCAL, rel), os.path.join(UNC, rel), name)

# ---------- 5) 终校验 ----------
print("== 5. 双侧终校验 ==")
bad2 = []
for rel, name in FILES:
    a = md5_of(os.path.join(LOCAL, rel))
    b = md5_of(os.path.join(UNC, rel))
    print(f"  {'OK ' if a == b else 'BAD'} {name:40s} {a[:12]}")
    if a != b:
        bad2.append(name)
if bad2:
    raise SystemExit(f"[FAIL] 终校验不一致: {bad2}")

html = io.open(os.path.join(UNC, r"frontend\dist\index.html"), encoding="utf-8").read()
assert "index-DSinavzX.js" in html and "index-vuWnffvU.css" in html, "index.html 未指向新 bundle"
print("[OK] 母本 index.html -> index-DSinavzX.js / index-vuWnffvU.css")
print(f"[DONE] rev24 投递完成 @ {TS}")
