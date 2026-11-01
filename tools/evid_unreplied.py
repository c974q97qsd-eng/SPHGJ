# -*- coding: utf-8 -*-
"""只读：列出还没回复的客户评论（挑一条做真实验证用）。"""
import sqlite3
from datetime import datetime

DB = r"\\Sphgj\sphgj\data\sphgj.db"
c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)

print("== replied=0 且近 10 天，按时间倒序 ==")
q = """SELECT comment_id, finder_id, export_id, create_time, nickname,
              substr(content,1,36), replied, deleted
       FROM comments
       WHERE COALESCE(replied,0)=0 AND COALESCE(deleted,0)=0 AND create_time > ?
       ORDER BY create_time DESC LIMIT 15"""
rows = c.execute(q, (int(datetime(2026, 10, 23).timestamp()),)).fetchall()
print("条数", len(rows))
for r in rows:
    t = datetime.fromtimestamp(r[3]).strftime("%m-%d %H:%M") if r[3] else "?"
    print("  %s | %s | %s | %s | %s | exp=%s" %
          (t, r[0], str(r[1])[:14], r[4], r[5], (r[2] or "")[:16]))
