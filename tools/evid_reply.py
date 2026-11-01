# -*- coding: utf-8 -*-
"""只读取证：回评成功/失败痕迹。"""
import sqlite3
import io
import sys

DB = r"\\Sphgj\sphgj\data\sphgj.db"
c = sqlite3.connect("file://%s?mode=ro&immutable=1" % DB.replace("\\", "/"), uri=True)

print("== own_comments by via ==")
for r in c.execute("SELECT via, COUNT(*) FROM own_comments GROUP BY via"):
    print("  ", r)

print("== own_comments 最近 8 条 ==")
for r in c.execute("SELECT finder_id,comment_id,via,created_at FROM own_comments "
                   "ORDER BY created_at DESC LIMIT 8"):
    print("  ", str(r[0])[:18], r[1], r[2], r[3])

print("== own_comment_pending 全部 ==")
for r in c.execute("SELECT id,finder_id,export_id,via,created_at,substr(content,1,24) "
                   "FROM own_comment_pending ORDER BY created_at"):
    print("  ", r[0], str(r[1])[:16], (r[2] or "")[:14], r[3], r[4], r[5])

print("== comments 里 replied=1 最近 10 条 ==")
for r in c.execute("SELECT comment_id,finder_id,create_time,replied,substr(content,1,20) "
                   "FROM comments WHERE replied=1 ORDER BY id DESC LIMIT 10"):
    print("  ", r)
