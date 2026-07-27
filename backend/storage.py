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
        self._stats_cache = None
        self._stats_cache_ts = 0.0
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")  # 锁等待 30s,避免立即抛异常导致连接泄漏/锁死
        return c

    def _init(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")  # WAL:读写不互斥,减少多 worker 并发写锁竞争
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
        c.execute("""CREATE TABLE IF NOT EXISTS live_window(
            account_id TEXT, window TEXT,
            ts REAL, audience INTEGER, gmv REAL,
            delta_a INTEGER, delta_g REAL,
            PRIMARY KEY(account_id, window))""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_write_count(
            account_id TEXT, date TEXT, count INTEGER,
            PRIMARY KEY(account_id, date))""")
        c.execute("""CREATE TABLE IF NOT EXISTS delete_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, comment_id TEXT, export_id TEXT,
            nickname TEXT, content TEXT, keyword TEXT, deleted_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_time ON comments(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_acc ON comments(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_time ON delete_logs(deleted_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_acc ON delete_logs(account_id)")
        # 迁移:为旧库补 deleted 列(已存在则忽略)
        try:
            c.execute("ALTER TABLE comments ADD COLUMN deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀
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
        """每账号统计:总数 / 已回 / 最近抓取时间。供仪表盘与账号管理用。缓存 60s。"""
        import time
        now = time.time()
        if self._stats_cache is not None and now - self._stats_cache_ts < 60:
            return self._stats_cache
        c = self._conn()
        rows = c.execute("""SELECT account_id,
                COUNT(*) AS total,
                SUM(CASE WHEN replied=1 THEN 1 ELSE 0 END) AS replied,
                MAX(fetched_at) AS last_fetched
            FROM comments GROUP BY account_id""").fetchall()
        c.close()
        self._stats_cache = [dict(account_id=r[0], total=r[1] or 0, replied=r[2] or 0,
                                  last_fetched=r[3]) for r in rows]
        self._stats_cache_ts = now
        return self._stats_cache

    # ---------- 直播大屏增值统计(每窗口固定一条,upsert 不堆积) ----------
    def save_window_state(self, account_id, window, ts, audience, gmv, delta_a, delta_g):
        c = self._conn()
        c.execute("""INSERT OR REPLACE INTO live_window
            (account_id,window,ts,audience,gmv,delta_a,delta_g) VALUES(?,?,?,?,?,?,?)""",
                  (account_id, window, ts, audience, gmv, delta_a, delta_g))
        c.commit()
        c.close()

    def load_window_states(self, account_id):
        """加载某账号各窗口的最近采样状态,worker 启动恢复用。返回 {window: dict}。"""
        c = self._conn()
        rows = c.execute(
            "SELECT window,ts,audience,gmv,delta_a,delta_g FROM live_window WHERE account_id=?",
            (account_id,)).fetchall()
        c.close()
        return {r[0]: dict(ts=r[1], audience=r[2], gmv=r[3], delta_a=r[4], delta_g=r[5]) for r in rows}

    # ---------- 防风控:写操作每日计数(持久化,跨重启) ----------
    def get_daily_write_count(self, account_id, date_str):
        c = self._conn()
        try:
            row = c.execute(
                "SELECT count FROM daily_write_count WHERE account_id=? AND date=?",
                (account_id, date_str)).fetchone()
            return row[0] if row else 0
        finally:
            c.close()

    def incr_daily_write_count(self, account_id, date_str):
        """递增某账号某日写计数,返回递增后的值。"""
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO daily_write_count(account_id,date,count) VALUES(?,?,0)",
                      (account_id, date_str))
            c.execute("UPDATE daily_write_count SET count=count+1 WHERE account_id=? AND date=?",
                      (account_id, date_str))
            c.commit()
            row = c.execute(
                "SELECT count FROM daily_write_count WHERE account_id=? AND date=?",
                (account_id, date_str)).fetchone()
            return row[0] if row else 0
        finally:
            c.close()

    # ---------- 自动删除记录(关键字命中删除日志,供删除记录卡片展示) ----------
    def log_delete(self, account_id, comment_id, nickname, content, keyword, export_id):
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (account_id,comment_id,export_id,nickname,content,keyword,deleted_at)
                VALUES(?,?,?,?,?,?,?)""",
                      (account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat()))
            c.commit()
        finally:
            c.close()

    def query_delete_logs(self, account_id=None, q=None, limit=200, offset=0):
        """分页查询删除记录,返回 (list[dict], total)。q 匹配 content/nickname/keyword。"""
        c = self._conn()
        try:
            where, args = [], []
            if account_id:
                where.append("account_id=?"); args.append(account_id)
            if q:
                where.append("(content LIKE ? OR nickname LIKE ? OR keyword LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            total = c.execute(f"SELECT COUNT(*) FROM delete_logs{clause}", args).fetchone()[0]
            rows = c.execute(
                f"""SELECT account_id,comment_id,export_id,nickname,content,keyword,deleted_at
                    FROM delete_logs{clause}
                    ORDER BY deleted_at DESC LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(account_id=r[0], comment_id=r[1], export_id=r[2], nickname=r[3],
                          content=r[4], keyword=r[5], deleted_at=r[6]) for r in rows]
            return items, total
        finally:
            c.close()

    def clear_delete_logs(self, account_id=None):
        c = self._conn()
        try:
            if account_id:
                c.execute("DELETE FROM delete_logs WHERE account_id=?", (account_id,))
            else:
                c.execute("DELETE FROM delete_logs")
            c.commit()
        finally:
            c.close()
