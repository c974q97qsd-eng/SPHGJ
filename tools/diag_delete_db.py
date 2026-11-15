# -*- coding: utf-8 -*-
"""只读核查母本 sphgj.db：今日写操作计数 / 最近删除记录 / 评论是否已从本地表移除。

注意：
1. 母本 config.json 的 db_path 指向 data/sphgj.db，不是默认的 comments.db。
2. UNC 上的 file: URI 在本机 sqlite 打不开，改成把 db + wal + shm 复制到本地
   快照再查，母本一个字节都不写。
3. 不硬编码任何真实 comment_id，最近删除的 ID 一律现查。
"""
import io
import os
import shutil
import sqlite3

DB = r"\\Sphgj\sphgj\data\sphgj.db"
SNAP_DIR = r"D:\SPHGJ\_db_snap"
OUT = r"D:\SPHGJ\_diag_del_db.txt"


def main():
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, "sphgj.db")
    for suffix in ("", "-wal", "-shm"):
        src = DB + suffix
        if os.path.exists(src):
            shutil.copy2(src, snap + suffix)
    c = sqlite3.connect(snap, timeout=10)
    out = []

    out.append("=== daily_write_count (今日) ===")
    try:
        rows = c.execute(
            "SELECT account_id, date, count FROM daily_write_count "
            "WHERE date=date('now','localtime') ORDER BY count DESC").fetchall()
        out.extend([repr(r) for r in rows] or ["(空)"])
    except Exception as e:
        out.append("ERR " + repr(e))

    out.append("")
    out.append("=== 最近 8 条 delete_logs ===")
    ids = []
    try:
        rows = c.execute(
            "SELECT comment_id, keyword, account_id, deleted_at FROM delete_logs "
            "ORDER BY deleted_at DESC LIMIT 8").fetchall()
        ids = [r[0] for r in rows[:5]]
        out.extend([repr(r) for r in rows] or ["(空)"])
    except Exception as e:
        out.append("ERR " + repr(e))

    out.append("")
    out.append("=== 最近 5 条删除的 comment_id 是否还在 comments 表 ===")
    try:
        q = ",".join("?" * len(ids))
        rows = c.execute("SELECT comment_id FROM comments WHERE comment_id IN (%s)" % q,
                         tuple(ids)).fetchall() if ids else []
        out.append("残留条数=%d %s" % (len(rows), repr(rows)))
    except Exception as e:
        out.append("ERR " + repr(e))

    out.append("")
    out.append("=== 所有表名 ===")
    try:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        out.append(", ".join(names))
    except Exception as e:
        out.append("ERR " + repr(e))
    c.close()
    io.open(OUT, "w", encoding="utf-8").write("\n".join(out))
    print("wrote " + OUT)


if __name__ == "__main__":
    main()
