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
        # 作品管理:作品本地库 + 抓取账本 + 本工具发出的评论(自动回复/自动评论/手动回复)
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            account_id TEXT, object_id TEXT,
            export_id TEXT, title TEXT, cover_url TEXT, cover_hash TEXT, cover_path TEXT,
            create_time INTEGER, read_count INTEGER, like_count INTEGER, comment_count INTEGER,
            forward_count INTEGER, fav_count INTEGER, follow_count INTEGER,
            visible_type INTEGER, sticky_op INTEGER, stat_sig TEXT,
            raw TEXT, updated_at TEXT,
            PRIMARY KEY(account_id, object_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS post_fetch_meta(
            account_id TEXT PRIMARY KEY,
            last_refresh TEXT, last_pages INTEGER,
            today_date TEXT, today_requests INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS own_comments(
            account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_acc ON posts(account_id)")
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

    def get_comment(self, comment_id):
        """取单条评论(删除前用于补充删除记录)。返回 dict 或 None。"""
        c = self._conn()
        try:
            r = c.execute(
                "SELECT account_id,export_id,nickname,content FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            return dict(account_id=r[0], export_id=r[1], nickname=r[2], content=r[3])
        finally:
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
    def query_comments(self, account_id=None, replied=None, q=None, limit=200, offset=0,
                       hide_own=False):
        """分页查询评论,返回 (list[dict], total)。"""
        c = self._conn()
        where, args = ["deleted=0"], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if replied is True:
            where.append("replied=1")
        elif replied is False:
            where.append("replied=0")
        if hide_own:
            # 隐藏本工具发出的评论(自动评论+置顶 / 自动回复 / 手动回复)
            where.append("comment_id NOT IN (SELECT comment_id FROM own_comments "
                         "UNION SELECT comment_id FROM auto_commented WHERE comment_id<>'')")
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

    # ==================== 作品管理 ====================
    def upsert_posts(self, account_id, rows):
        """批量入库作品(抓取器用)。rows 为 post_fetcher._extract_post 的产物。"""
        c = self._conn()
        try:
            now = datetime.now().isoformat()
            for r in rows:
                sig = f"{r['read_count']}:{r['like_count']}:{r['comment_count']}"
                c.execute("""INSERT INTO posts(account_id,object_id,export_id,title,cover_url,cover_hash,cover_path,
                             create_time,read_count,like_count,comment_count,forward_count,fav_count,follow_count,
                             visible_type,sticky_op,stat_sig,raw,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,object_id) DO UPDATE SET
                      export_id=excluded.export_id, title=excluded.title,
                      cover_url=excluded.cover_url, cover_hash=excluded.cover_hash, cover_path=excluded.cover_path,
                      create_time=excluded.create_time, read_count=excluded.read_count, like_count=excluded.like_count,
                      comment_count=excluded.comment_count, forward_count=excluded.forward_count,
                      fav_count=excluded.fav_count, follow_count=excluded.follow_count,
                      visible_type=excluded.visible_type, sticky_op=excluded.sticky_op,
                      stat_sig=excluded.stat_sig, raw=excluded.raw, updated_at=excluded.updated_at""",
                    (account_id, r["object_id"], r["export_id"], r["title"], r["cover_url"], r["cover_hash"],
                     r.get("cover_path") or "", r["create_time"], r["read_count"], r["like_count"],
                     r["comment_count"], r["forward_count"], r["fav_count"], r["follow_count"],
                     r["visible_type"], r["sticky_op"], sig, r["raw"], now))
            c.commit()
        finally:
            c.close()

    def posts_known_and_unchanged(self, account_id, id_sig_pairs):
        """早停判定:这批作品全部已入库且 stat_sig 都没变。"""
        if not id_sig_pairs:
            return False
        c = self._conn()
        try:
            want = dict(id_sig_pairs)
            ph = ",".join("?" * len(want))
            rows = c.execute(
                f"SELECT object_id, stat_sig FROM posts WHERE account_id=? AND object_id IN ({ph})",
                [account_id] + list(want.keys())).fetchall()
            have = dict(rows)
            return all(have.get(oid) == sig for oid, sig in want.items())
        finally:
            c.close()

    def get_post(self, account_id, object_id):
        c = self._conn()
        try:
            r = c.execute("SELECT object_id, export_id, visible_type, sticky_op, title FROM posts "
                          "WHERE account_id=? AND object_id=?", (account_id, object_id)).fetchone()
            if not r:
                return None
            return dict(object_id=r[0], export_id=r[1], visible_type=r[2], sticky_op=r[3], title=r[4])
        finally:
            c.close()

    def update_post_flags(self, account_id, object_id, visible_type=None, sticky_op=None):
        """本地状态直接更新(写操作成功后调用,不回抓)。"""
        sets, args = [], []
        if visible_type is not None:
            sets.append("visible_type=?"); args.append(int(visible_type))
        if sticky_op is not None:
            sets.append("sticky_op=?"); args.append(int(sticky_op))
        if not sets:
            return
        sets.append("updated_at=?"); args.append(datetime.now().isoformat())
        args.extend([account_id, object_id])
        c = self._conn()
        try:
            c.execute(f"UPDATE posts SET {', '.join(sets)} WHERE account_id=? AND object_id=?", args)
            c.commit()
        finally:
            c.close()

    _SORTS = {"create_time": "create_time", "read_count": "read_count", "like_count": "like_count",
              "comment_count": "comment_count", "forward_count": "forward_count", "fav_count": "fav_count"}

    def query_posts(self, account_id=None, q=None, visible=None, date_from=None, date_to=None,
                    sort="create_time", order="desc", limit=50, offset=0):
        """分页查询作品。visible: public/hidden/sticky;date_from/date_to: YYYY-MM-DD(按发布时间)。

        sticky 作品(置顶)永远排在最前,组内再按所选排序。
        """
        col = self._SORTS.get(sort, "create_time")
        direc = "ASC" if str(order).lower() == "asc" else "DESC"
        where, args = [], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if q:
            where.append("title LIKE ?"); args.append(f"%{q}%")
        if visible == "public":
            where.append("visible_type=1")
        elif visible == "hidden":
            where.append("visible_type=3")
        elif visible == "follow":
            where.append("visible_type=2")
        if visible == "sticky":
            pass  # sticky 仅为筛选已置顶 -> 与其它 visible 互斥,单独处理
        try:
            if date_from:
                d = datetime.strptime(date_from, "%Y-%m-%d")
                where.append("create_time>=?"); args.append(int(d.timestamp()))
            if date_to:
                import time as _t
                d = datetime.strptime(date_to, "%Y-%m-%d").timestamp() + 86399
                where.append("create_time<=?"); args.append(int(d))
        except ValueError:
            pass
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sticky_clause = "sticky_op=2" if visible == "sticky" else None
        if sticky_clause:
            clause = ((" WHERE " + sticky_clause) if not where
                      else clause + " AND " + sticky_clause)
        c = self._conn()
        try:
            total = c.execute(f"SELECT COUNT(*) FROM posts{clause}", args).fetchone()[0]
            order_expr = (f"sticky_op=2 DESC, {col} {direc}" if visible != "sticky"
                          else f"{col} {direc}")
            rows = c.execute(
                f"""SELECT account_id,object_id,export_id,title,cover_url,cover_path,
                            create_time,read_count,like_count,comment_count,forward_count,fav_count,
                            visible_type,sticky_op,updated_at
                     FROM posts{clause} ORDER BY {order_expr} LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(account_id=r[0], object_id=r[1], export_id=r[2], title=r[3],
                          cover_url=r[4], cover_path=r[5], create_time=r[6], read_count=r[7],
                          like_count=r[8], comment_count=r[9], forward_count=r[10],
                          fav_count=r[11], visible_type=r[12], sticky_op=r[13],
                          is_hidden=(r[12] == 3), is_sticky=(r[13] == 2),
                          updated_at=r[14]) for r in rows]
            return items, total
        finally:
            c.close()

    # ---------- 抓取账本 ----------
    def get_post_fetch_meta(self, account_id):
        c = self._conn()
        try:
            r = c.execute("SELECT last_refresh,last_pages,today_date,today_requests "
                          "FROM post_fetch_meta WHERE account_id=?", (account_id,)).fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            if not r:
                return dict(last_refresh=None, last_pages=0, today_requests=0)
            return dict(last_refresh=r[0], last_pages=r[1] or 0,
                        today_requests=(r[3] or 0) if r[2] == today else 0)
        finally:
            c.close()

    def set_post_fetch_meta(self, account_id, last_refresh=None, last_pages=None, add_requests=0):
        today = datetime.now().strftime("%Y-%m-%d")
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO post_fetch_meta(account_id,last_refresh,last_pages,today_date,today_requests) "
                      "VALUES(?,?,?,?,0)", (account_id, None, 0, today))
            if add_requests > 0:
                c.execute("UPDATE post_fetch_meta SET today_requests=CASE WHEN today_date=? THEN today_requests+? ELSE ? END "
                          "WHERE account_id=?", (today, add_requests, add_requests, account_id))
            if last_refresh is not None:
                c.execute("UPDATE post_fetch_meta SET last_refresh=?, last_pages=? WHERE account_id=?",
                          (last_refresh, last_pages or 0, account_id))
            c.commit()
        finally:
            c.close()

    # ---------- 本工具发出的评论 ----------
    def mark_own_comment(self, account_id, comment_id, via):
        if not comment_id:
            return
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO own_comments(account_id,comment_id,via,created_at) VALUES(?,?,?,?)",
                      (account_id, comment_id, via, datetime.now().isoformat()))
            c.commit()
        finally:
            c.close()
