"""SQLite 存储:评论 + 视频评论数(增量对比用)+ 自动评论记录。

表:
  comments        评论(account_id/export_id/comment_id UNIQUE/nickname/content/create_time/replied ...)
  video_stats     视频评论数(account_id+export_id 主键)--增量对比:count 没变则跳过抓取
  auto_commented  已自动评论的视频(避免重发)
"""
import os
import json
import sqlite3
from datetime import datetime


class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        c = self._conn()
        c.execute("""CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, export_id TEXT, comment_id TEXT UNIQUE,
            nickname TEXT, content TEXT, head_url TEXT, create_time INTEGER,
            like_count INTEGER, read_flag INTEGER, replied INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            raw TEXT, fetched_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS video_stats(
            account_id TEXT, export_id TEXT, comment_count INTEGER, updated_at TEXT,
            PRIMARY KEY(account_id, export_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS auto_commented(
            account_id TEXT, export_id TEXT, comment_id TEXT, commented_at TEXT,
            PRIMARY KEY(account_id, export_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_time ON comments(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_acc ON comments(account_id)")
        # 迁移:为旧库补 deleted 列(已存在则忽略)
        try:
            c.execute("ALTER TABLE comments ADD COLUMN deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        c.commit()
        c.close()

    def upsert_comment(self, account_id, export_id, comment):
        c = self._conn()
        cid = comment.get("commentId")
        # INSERT OR IGNORE:新评论插入(replied=0);已存在的不动 replied
        c.execute("""INSERT OR IGNORE INTO comments
            (account_id,export_id,comment_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, export_id, cid, comment.get("commentNickname"),
             comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0),
             int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, 0,
             json.dumps(comment, ensure_ascii=False), datetime.now().isoformat()))
        # UPDATE 其他字段(不覆盖 replied)
        c.execute("""UPDATE comments SET nickname=?,content=?,head_url=?,create_time=?,like_count=?,read_flag=?,raw=?,fetched_at=?
            WHERE comment_id=?""",
            (comment.get("commentNickname"), comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0), int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, json.dumps(comment, ensure_ascii=False),
             datetime.now().isoformat(), cid))
        c.commit()
        c.close()

    def is_comment_exists(self, comment_id):
        c = self._conn()
        r = c.execute("SELECT 1 FROM comments WHERE comment_id=?", (comment_id,)).fetchone()
        c.close()
        return r is not None

    def mark_replied(self, comment_id):
        c = self._conn()
        c.execute("UPDATE comments SET replied=1 WHERE comment_id=?", (comment_id,))
        c.commit()
        c.close()

    def is_replied(self, comment_id):
        c = self._conn()
        r = c.execute("SELECT replied FROM comments WHERE comment_id=?", (comment_id,)).fetchone()
        c.close()
        return bool(r and r[0])

    def mark_deleted(self, comment_id):
        c = self._conn()
        c.execute("UPDATE comments SET deleted=1 WHERE comment_id=?", (comment_id,))
        c.commit()
        c.close()

    def is_deleted(self, comment_id):
        c = self._conn()
        r = c.execute("SELECT deleted FROM comments WHERE comment_id=?", (comment_id,)).fetchone()
        c.close()
        return bool(r and r[0])

    def get_video_comment_count(self, account_id, export_id):
        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        c.close()
        return r[0] if r else None

    def set_video_comment_count(self, account_id, export_id, count):
        c = self._conn()
        c.execute("""INSERT OR REPLACE INTO video_stats(account_id,export_id,comment_count,updated_at)
            VALUES(?,?,?,?)""", (account_id, export_id, count, datetime.now().isoformat()))
        c.commit()
        c.close()

    def delete_comment(self, comment_id):
        c = self._conn()
        c.execute("DELETE FROM comments WHERE comment_id=?", (comment_id,))
        c.commit()
        c.close()

    def is_auto_commented(self, account_id, export_id):
        c = self._conn()
        r = c.execute("SELECT 1 FROM auto_commented WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        c.close()
        return r is not None

    def set_auto_commented(self, account_id, export_id, comment_id):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO auto_commented(account_id,export_id,comment_id,commented_at) VALUES(?,?,?,?)",
                  (account_id, export_id, comment_id, datetime.now().isoformat()))
        c.commit()
        c.close()

    def recent_comments(self, account_id=None, limit=200):
        """旧接口:最近评论(元组)。保留给导出/兼容。"""
        c = self._conn()
        cols = "account_id,export_id,comment_id,nickname,content,head_url,create_time,like_count,replied"
        if account_id:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE account_id=? AND deleted=0 ORDER BY create_time DESC LIMIT ?",
                (account_id, limit)).fetchall()
        else:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE deleted=0 ORDER BY create_time DESC LIMIT ?",
                (limit,)).fetchall()
        c.close()
        return rows

    # ---------- 新增:分页查询 + 账号统计(供 API 层) ----------
    def query_comments(self, account_id=None, replied=None, q=None, limit=200, offset=0):
        """分页查询评论,返回 (list[dict], total)。"""
        c = self._conn()
        where, args = ["deleted=0"], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if replied is True:
            where.append("replied=1")
        elif replied is False:
            where.append("replied=0")
        if q:
            where.append("(content LIKE ? OR nickname LIKE ?)"); args.extend([f"%{q}%", f"%{q}%"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = c.execute(f"SELECT COUNT(*) FROM comments{clause}", args).fetchone()[0]
        rows = c.execute(
            f"""SELECT account_id,export_id,comment_id,nickname,content,head_url,
                      create_time,like_count,replied,fetched_at
               FROM comments{clause}
               ORDER BY create_time DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        c.close()
        items = [dict(account_id=r[0], export_id=r[1], comment_id=r[2], nickname=r[3],
                      content=r[4], head_url=r[5], create_time=r[6], like_count=r[7],
                      replied=bool(r[8]), fetched_at=r[9]) for r in rows]
        return items, total

    def account_stats(self):
        """每账号统计:总数 / 已回 / 最近抓取时间。供仪表盘与账号管理用。"""
        c = self._conn()
        rows = c.execute("""SELECT account_id,
                COUNT(*) AS total,
                SUM(CASE WHEN replied=1 THEN 1 ELSE 0 END) AS replied,
                MAX(fetched_at) AS last_fetched
            FROM comments GROUP BY account_id""").fetchall()
        c.close()
        return [dict(account_id=r[0], total=r[1] or 0, replied=r[2] or 0,
                     last_fetched=r[3]) for r in rows]
