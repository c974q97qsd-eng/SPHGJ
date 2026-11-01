# -*- coding: utf-8 -*-
"""只读:历史上自动评论打出去时,视频已经是多久以前发的?(判断要不要加「只评论新视频」闸门)"""
import datetime
import json
import sqlite3

DB = r"\\Sphgj\\sphgj\\data\\sphgj.db".replace("\\\\", "\\")
DB = "\\\\Sphgj\\sphgj\\data\\sphgj.db"
cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
c.row_factory = sqlite3.Row

for a in cfg["accounts"]:
    fid = a.get("_log_finder_id") or ""
    if not fid:
        continue
    rows = c.execute(
        "SELECT ac.commented_at, p.create_time FROM auto_commented ac "
        "LEFT JOIN posts p ON p.finder_id=ac.finder_id AND p.export_id=ac.export_id "
        "WHERE ac.finder_id=?", (fid,)).fetchall()
    if not rows:
        continue
    ages = []
    for r in rows:
        if not r["create_time"]:
            continue
        try:
            ct = datetime.datetime.fromtimestamp(int(r["create_time"]))
            at = datetime.datetime.fromisoformat(str(r["commented_at"])[:19])
            ages.append((at - ct).total_seconds() / 86400.0)
        except Exception:
            pass
    if not ages:
        print("%-14s 无可比数据" % a["id"])
        continue
    ages.sort()
    n = len(ages)
    old7 = sum(1 for x in ages if x > 7)
    old30 = sum(1 for x in ages if x > 30)
    print("%-14s 样本%4d  中位%.1f天  平均%.1f天  >7天 %d(%.0f%%)  >30天 %d(%.0f%%)  最大%.0f天" % (
        a["id"], n, ages[n // 2], sum(ages) / n, old7, 100.0 * old7 / n, old30, 100.0 * old30 / n, ages[-1]))
