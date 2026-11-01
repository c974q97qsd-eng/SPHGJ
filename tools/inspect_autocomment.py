# -*- coding: utf-8 -*-
"""只读:每个账号(按 finder_id)的自动评论覆盖情况 + 7 号作品时间线。"""
import datetime
import json
import sqlite3

DB = r"\\Sphgj\sphgj\data\sphgj.db"
cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row


def ts(v):
    try:
        return datetime.datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


print("%-14s %-18s %6s %6s %6s %s" % ("account", "finder_id", "posts", "自动评论", "own", "最新作品"))
for a in cfg["accounts"]:
    fid = a.get("_log_finder_id") or a.get("locked_finder_id") or ""
    if not fid:
        continue
    p = c.execute("SELECT COUNT(*) n, MAX(create_time) mx FROM posts WHERE finder_id=?", (fid,)).fetchone()
    ac = c.execute("SELECT COUNT(*) FROM auto_commented WHERE finder_id=?", (fid,)).fetchone()[0]
    oc = c.execute("SELECT COUNT(*) FROM own_comments WHERE finder_id=?", (fid,)).fetchone()[0]
    print("%-14s %-18s %6d %6d %6d %s" % (a["id"], fid[:16], p["n"] or 0, ac, oc, ts(p["mx"])))

a7 = [a for a in cfg["accounts"] if a["id"] == "uspoloassn7"][0]
fid = a7["_log_finder_id"]
print("\n== uspoloassn7 作品按发布时间(最近 10) ==")
for r in c.execute("SELECT object_id, export_id, create_time, comment_count FROM posts "
                   "WHERE finder_id=? ORDER BY create_time DESC LIMIT 10", (fid,)):
    print("   %s  %s  cc=%s  %s" % (ts(r["create_time"]), r["object_id"][:44],
                                    r["comment_count"], (r["export_id"] or "")[:40]))
print("\n== uspoloassn7 最近自动评论时间 ==")
for r in c.execute("SELECT export_id, commented_at FROM auto_commented WHERE finder_id=? "
                   "ORDER BY commented_at DESC LIMIT 6", (fid,)):
    print("   %s  %s" % (r["commented_at"], (r["export_id"] or "")[:44]))
