# -*- coding: utf-8 -*-
"""只读：列出 replied=1 的全部评论，供分类与补回复。"""
import sqlite3
import datetime

DB = r"\\Sphgj\sphgj\data\sphgj.db"
c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)

# 已知真回复成功的：09-27「有的」+ 今天 02:37 验证的那条
DONE = {"15045101693521758355"}

rows = c.execute("""SELECT comment_id, finder_id, account_id, export_id,
                           create_time, nickname, content, replied
                    FROM comments WHERE replied=1
                    ORDER BY create_time ASC""").fetchall()
print("replied=1 共 %d 条" % len(rows))
for i, r in enumerate(rows, 1):
    t = datetime.datetime.fromtimestamp(r[4]).strftime("%m-%d %H:%M") if r[4] else "?"
    flag = "  [已真回]" if r[0] in DONE else ""
    print("%2d | %s | %s | %s | %s%s" % (i, t, r[0], r[5], (r[6] or "")[:34], flag))

print()
print("需要补回的 =", len([r for r in rows if r[0] not in DONE]))
print()
print("== 按账号分布 ==")
for r in c.execute("SELECT account_id, COUNT(*) FROM comments WHERE replied=1 GROUP BY account_id"):
    print("  ", r)
