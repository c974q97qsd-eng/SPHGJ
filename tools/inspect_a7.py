# -*- coding: utf-8 -*-
"""只读:uspoloassn7 的作品时间分布 + 自动评论历史,判断「修好之后会不会对老视频刷评论」。"""
import datetime
import json
import sqlite3

DB = r"\\Sphgj\sphgj\data\sphgj.db"
cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
acc = [a for a in cfg["accounts"] if a["id"] == "uspoloassn7"][0]
fid = acc.get("_log_finder_id") or acc.get("locked_finder_id")
print("uspoloassn7 finder_id =", fid[:24] + "..." if fid else None)

c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row

print("\n== 各表按 finder_id 计数 ==")
for t in ("videos", "video_fetch_state", "own_comments", "comments"):
    try:
        n = c.execute("SELECT COUNT(*) FROM %s WHERE finder_id=?" % t, (fid,)).fetchone()[0]
        print("  %-20s %s" % (t, n))
    except Exception as e:
        print("  %-20s ERR %s" % (t, e))

print("\n== uspoloassn7 作品 createTime 分布(最近 12 条) ==")
try:
    rows = c.execute(
        "SELECT object_id, create_time FROM videos WHERE finder_id=? "
        "ORDER BY create_time DESC LIMIT 12", (fid,)).fetchall()
    for r in rows:
        ts = r["create_time"]
        try:
            s = datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            s = str(ts)
        print("   ", r["object_id"][:52], s)
    tot = c.execute("SELECT COUNT(*) FROM videos WHERE finder_id=?", (fid,)).fetchone()[0]
    print("   合计作品:", tot)
except Exception as e:
    print("   ERR", e)

print("\n== 该账号 own_comments 按 via ==")
try:
    for r in c.execute("SELECT via, COUNT(*) n FROM own_comments WHERE finder_id=? GROUP BY via", (fid,)):
        print("   ", r["via"], r["n"])
except Exception as e:
    print("   ERR", e)

print("\n== 全部账号 own_comments 按 via ==")
for r in c.execute("SELECT via, COUNT(*) n FROM own_comments GROUP BY via"):
    print("   ", r["via"], r["n"])
