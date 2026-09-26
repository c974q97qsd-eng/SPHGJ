"""SQLite 存储:评论 + 视频评论数(增量对比用)+ 自动评论记录。

表:
  comments        评论(account_id/export_id/comment_id UNIQUE/nickname/content/create_time/replied ...)
  video_stats     视频评论数(account_id+export_id 主键)--增量对比:count 没变则跳过抓取
  auto_commented  已自动评论的视频(避免重发)
  own_comments    本工具发出的评论登记(自动评论/自动回复/手动回复,隐藏与溯源用)
  own_comment_pending  发出但接口没回 comment_id 的评论,抓取时按 export_id+内容补登
"""
import logging
import os
import json
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger("sphgj")


class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        self._stats_cache = None
        self._stats_cache_ts = 0.0
        self._own_ident_cache = {}
        self._own_ident_ts = 0.0
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
            author_id TEXT,
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
            today_date TEXT, today_requests INTEGER,
            full_synced INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS own_comments(
            account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT)""")
        # 待确认登记:发评论/回复成功但接口没回 comment_id 时先记下,
        # 下一轮抓到该评论(comment_list 会带回自己发的评论)再补登真实 id
        c.execute("""CREATE TABLE IF NOT EXISTS own_comment_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, export_id TEXT, content TEXT, via TEXT, created_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_own_pending_acc ON own_comment_pending(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_acc ON posts(account_id)")
        # 迁移:为旧库补 deleted 列(已存在则忽略)
        try:
            c.execute("ALTER TABLE comments ADD COLUMN deleted INTEGER DEFAULT 0")
        except Exception:
            pass
        # 迁移:补 full_synced(作品是否已完整抓取过一次;0=未全量,下次刷新一路翻到底补全)
        try:
            c.execute("ALTER TABLE post_fetch_meta ADD COLUMN full_synced INTEGER DEFAULT 0")
        except Exception:
            pass
        # 迁移:补 author_id(评论作者的视频号 id,接口 raw.username。用于精准识别「自己发出的评论」)
        try:
            c.execute("ALTER TABLE comments ADD COLUMN author_id TEXT")
        except Exception:
            pass
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀
        c.commit()
        c.close()
        # 历史评论 raw 里本就带作者 id,补一次(只处理缺列的行)
        try:
            self._backfill_author_ids()
        except Exception:
            pass

    def upsert_comment(self, account_id, export_id, comment):
        c = self._conn()
        cid = comment.get("commentId")
        # 作者身份:接口每条评论都带 username(作者的视频号 id),同一次响应里与评论同源,不会错位
        author_id = comment.get("username") or comment.get("commentUsername") or ""
        # INSERT OR IGNORE:新评论插入(replied=0);已存在的不动 replied
        c.execute("""INSERT OR IGNORE INTO comments
            (account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, export_id, cid, author_id, comment.get("commentNickname"),
             comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0),
             int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, 0,
             json.dumps(comment, ensure_ascii=False), datetime.now().isoformat()))
        # UPDATE 其他字段(不覆盖 replied)
        c.execute("""UPDATE comments SET author_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE author_id END,
                nickname=?,content=?,head_url=?,create_time=?,like_count=?,read_flag=?,raw=?,fetched_at=?
            WHERE comment_id=?""",
            (author_id, author_id, comment.get("commentNickname"), comment.get("commentContent"), comment.get("commentHeadurl"),
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

    def get_video_fetch_state(self, account_id, export_id):
        """一次取回该视频的抓取状态,用于早停判定。返回 dict:
            seen         本地是否已有该视频记录(无 => 新视频)
            cc           上次记录的评论数(None = 无记录)
            has_comments 该视频在 comments 表是否已有行

        为什么单独开这个方法:早停「评论数没增加就跳过」有致命前提 —— 本地必须
        【真的抓到过评论】。历史上曾把接口返回的 commentCount=0(字段缺失/请求失败
        被 or 0 吞掉)写进 video_stats,于是 cc=0 的视频连 comment_list 都不发,
        永远停在 0 评论。所以判据里必须带上 has_comments。
        """
        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        cc = r[0] if r else None
        has = c.execute("SELECT 1 FROM comments WHERE account_id=? AND export_id=? LIMIT 1",
                        (account_id, export_id)).fetchone() is not None
        c.close()
        return dict(seen=r is not None, cc=cc, has_comments=has)

    def set_video_comment_count(self, account_id, export_id, count):
        """记录该视频本次看到的评论数。

        单调性保护:接口偶发把 commentCount 返回成 0(字段缺失 / 请求失败),
        直接写回会把快照打回 0 —— 而 0 会让下一轮走「没评论」分支直接跳过,
        等于永久锁死(2026-09-26 线上事故根因)。评论数不会无故减少,
        故新值更小时保留旧值;但允许 0 -> 正数 的正常增长。
        """
        c = self._conn()
        try:
            new = int(count or 0)
        except (TypeError, ValueError):
            new = 0
        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        old = r[0] if r else None
        if old is not None and new < old:
            new = old            # 脏值:保留旧值,不回退
        c.execute("""INSERT OR REPLACE INTO video_stats(account_id,export_id,comment_count,updated_at)
            VALUES(?,?,?,?)""", (account_id, export_id, new, datetime.now().isoformat()))
        c.commit()
        c.close()
        return new

    def reset_video_comment_count(self, account_id=None, export_id=None):
        """清掉视频的评论数快照,强制下一轮重新抓取。返回清除条数。

        用于修历史脏数据:video_stats 里记了 count 但 comments 表一条都没有,
        说明该视频从未真正抓到过评论(被 0 值快照锁死),必须复位重抓。
        account_id / export_id 为 None 表示不限定。
        """
        c = self._conn()
        where, args = [], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if export_id:
            where.append("export_id=?"); args.append(export_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        cur = c.execute(f"DELETE FROM video_stats{clause}", args)
        n = cur.rowcount or 0
        c.commit()
        c.close()
        return n

    def reset_stale_video_counts(self):
        """复位「评论数快照 > 0 但从未抓到过评论」的脏视频。返回 (受影响账号数, 清除条数)。

        判据只有两条,刻意【不依赖 posts 表】:
          a) comments 表里没有该 (account_id, export_id) 的行 —— 从未抓到过评论
          b) video_stats.comment_count > 0 —— 接口明确报过「这条视频有评论」
        两条同时成立 => 接口说有评论、我们却一条都没抓到,必然是脏快照锁死。

        为什么不用 posts.comment_count 交叉验证(2026-09-26 踩过):
        posts 表是按账号增量抓的,未抓过作品的账号在 posts 里
        一条记录都没有,拿它当过滤器会把整个账号漏掉。
        cc=0 的视频不复位:那是「接口说的确没评论」的正常状态,复位了会白跑一圈。
        """
        c = self._conn()
        rows = c.execute("""SELECT vs.account_id, vs.export_id FROM video_stats vs
            WHERE vs.comment_count > 0
              AND NOT EXISTS (
                SELECT 1 FROM comments cm
                WHERE cm.account_id = vs.account_id AND cm.export_id = vs.export_id)""").fetchall()
        c.close()
        if not rows:
            return 0, 0
        by_acc = {}
        for acc, eid in rows:
            by_acc.setdefault(acc, []).append(eid)
        total = 0
        for acc, eids in by_acc.items():
            for i in range(0, len(eids), 500):
                chunk = eids[i:i + 500]
                cc = self._conn()
                qs = ",".join("?" * len(chunk))
                total += cc.execute(
                    f"DELETE FROM video_stats WHERE account_id=? AND export_id IN ({qs})",
                    [acc] + chunk).rowcount or 0
                cc.commit()
                cc.close()
        return len(by_acc), total

    def video_fetch_health(self):
        """诊断:返回每个账号的「评论抓取健康度」。供排查「新增0评论」用。

        关键指标 never_fetched = 快照说有评论、但 comments 表一条都没有的视频数。
        该值长期 > 0 且持续增长 => 早停判定又把视频锁死了。
        """
        c = self._conn()
        rows = c.execute("""SELECT vs.account_id,
                COUNT(*) AS videos,
                SUM(CASE WHEN vs.comment_count > 0 THEN 1 ELSE 0 END) AS with_cc,
                SUM(CASE WHEN vs.comment_count > 0 AND NOT EXISTS (
                        SELECT 1 FROM comments cm
                        WHERE cm.account_id = vs.account_id AND cm.export_id = vs.export_id)
                    THEN 1 ELSE 0 END) AS never_fetched,
                MAX(vs.updated_at) AS last_scan
            FROM video_stats vs GROUP BY vs.account_id""").fetchall()
        c.close()
        return [dict(account_id=r[0], videos=r[1] or 0, with_cc=r[2] or 0,
                     never_fetched=r[3] or 0, last_scan=r[4]) for r in rows]

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
                       hide_own=False, own_map=None):
        """分页查询评论,返回 (list[dict], total)。

        hide_own=True 隐藏「自己发出的评论」,判据(并集,见 _own_exclusion):
          1) comment_id 在本工具登记表(自动评论+置顶 / 自动回复 / 手动回复)
          2) 作者就是本账号自己 —— 接口给的作者视频号 id 命中 own_map[acc]['ids'],
             或昵称命中 ['names'](账号改名/旧数据 raw 缺 id 时兜底)
        """
        c = self._conn()
        where, args = ["deleted=0"], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if replied is True:
            where.append("replied=1")
        elif replied is False:
            where.append("replied=0")
        if hide_own:
            # 隐藏自己发出的评论:本工具登记过的 id + 作者身份命中本账号
            _sql, _a = self._own_exclusion(own_map)
            where.append(_sql); args.extend(_a)
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
        try:
            c = self._conn()
            rows = c.execute("""SELECT account_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN replied=1 THEN 1 ELSE 0 END) AS replied,
                    MAX(fetched_at) AS last_fetched
                FROM comments GROUP BY account_id""").fetchall()
            c.close()
        except Exception as e:
            # 读路径兜底:库被外部工具只读打开 / UNC 访问抖动会抛
            # sqlite3.DatabaseError(file is not a database),不能让整页 /api/stats 500。
            # 返回上一次缓存(可能为空),并保留 _stats_cache_ts 让 60s 后自然重试。
            logger.warning(f"account_stats 读取失败,返回缓存: {e}")
            return self._stats_cache or []
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

    def posts_all_known(self, account_id, object_ids):
        """早停判定:这一页的作品是否已全部在本地库。

        列表按时间倒序(置顶在前),新作品必然出现在更靠前的页,所以
        「本页 id 全部已知」即说明其后都是更老的已入库作品,可以停。
        刻意不用 stat_sig(播放/赞/评签名)当判据:统计数天天在变,用它会导致永远停不下来。
        """
        ids = [i for i in object_ids if i]
        if not ids:
            return False
        c = self._conn()
        try:
            ph = ",".join("?" * len(ids))
            n = c.execute(
                f"SELECT COUNT(*) FROM posts WHERE account_id=? AND object_id IN ({ph})",
                [account_id] + ids).fetchone()[0]
            return int(n) >= len(ids)
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
            r = c.execute("SELECT last_refresh,last_pages,today_date,today_requests,full_synced "
                          "FROM post_fetch_meta WHERE account_id=?", (account_id,)).fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            if not r:
                return dict(last_refresh=None, last_pages=0, today_requests=0, full_synced=0)
            return dict(last_refresh=r[0], last_pages=r[1] or 0,
                        today_requests=(r[3] or 0) if r[2] == today else 0,
                        full_synced=int(r[4] or 0))
        finally:
            c.close()

    def set_post_fetch_meta(self, account_id, last_refresh=None, last_pages=None, add_requests=0,
                            full_synced=None):
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
            if full_synced is not None:
                c.execute("UPDATE post_fetch_meta SET full_synced=? WHERE account_id=?",
                          (1 if full_synced else 0, account_id))
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _own_exclusion(own_map=None):
        """生成「排除自己发出的评论」的 WHERE 片段,返回 (sql, args)。

        判据一:comment_id 在本工具登记表里(自动评论+置顶 / 自动回复 / 手动回复)。
        判据二:作者身份与本账号自己一致 —— 优先视频号 id(权威,作者改名也不受影响),
               昵称兜底(旧数据 raw 里可能没有作者 id)。
        author_id 为空、昵称也对不上时无法判定 -> 保持显示,绝不误伤客户评论。
        """
        parts = ["comment_id IN (SELECT comment_id FROM own_comments WHERE COALESCE(comment_id,'')<>''"
                 " UNION SELECT comment_id FROM auto_commented WHERE COALESCE(comment_id,'')<>'')"]
        args = []
        for acc, ident in sorted((own_map or {}).items()):
            ids = sorted({x for x in (ident.get("ids") or []) if x})
            names = sorted({x for x in (ident.get("names") or []) if x})
            cond, sub = [], []
            if ids:
                cond.append("COALESCE(author_id,'') IN (%s)" % ",".join(["?"] * len(ids)))
                sub.extend(ids)
            if names:
                cond.append("nickname IN (%s)" % ",".join(["?"] * len(names)))
                sub.extend(names)
            if cond:
                # 注意参数顺序必须与 SQL 里占位符出现的顺序一致:account_id 在最前
                parts.append("(account_id=? AND (%s))" % " OR ".join(cond))
                args.append(acc)
                args.extend(sub)
        return "(NOT (%s))" % " OR ".join(parts), args

    def own_identities_from_library(self, min_count=10, min_ratio=0.2,
                                    allow_ids=None, allow_names=None, ttl=60):
        """从库内统计每个账号「自己」的作者 id / 昵称(缓存 ttl 秒)。

        用途:账号被删掉重建(account_id 变了)后,旧 id 上还留着以前自己发的评论,
        配置里已经没有这个账号,靠统计也能认出来。

        依据:一个账号收到的评论里,自己发出的评论数远多于任何单个客户;
        取该账号出现次数最多、且 >= min_count 条、且占比 >= min_ratio 的作者 id 与昵称。

        allow_ids / allow_names:已知身份白名单(调用方传 accounts 里所有账号的
        finder_id / 名称)。给了就只认命中白名单的项 —— 否则一个刷屏客户
        (80% 的评论都是他)会被误判成「自己」,把他的真实评论也藏起来。
        """
        import time
        now = time.time()
        ckey = (min_count, min_ratio,
                None if allow_ids is None else tuple(sorted(allow_ids)),
                None if allow_names is None else tuple(sorted(allow_names)))
        cached = self._own_ident_cache.get(ckey)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        out = {}
        c = self._conn()
        try:
            for col, key, allow in (("author_id", "ids", allow_ids),
                                    ("nickname", "names", allow_names)):
                rows = c.execute(
                    f"""SELECT account_id, {col}, COUNT(*) FROM comments
                        WHERE deleted=0 AND COALESCE({col},'')<>''
                        GROUP BY account_id, {col}""").fetchall()
                total = {}
                for acc, _v, n in rows:
                    total[acc] = total.get(acc, 0) + n
                best = {}
                for acc, v, n in rows:
                    if allow is not None and v not in allow:
                        continue
                    if n < min_count or n < total[acc] * min_ratio:
                        continue
                    if acc not in best or n > best[acc][1]:
                        best[acc] = (v, n)
                for acc, (v, _n) in best.items():
                    out.setdefault(acc, {"ids": set(), "names": set()})[key].add(v)
        finally:
            c.close()
        self._own_ident_cache[ckey] = (now, out)
        self._own_ident_ts = now
        return out

    def _backfill_author_ids(self):
        """把历史评论 raw 里的作者视频号 id 回填到 author_id 列(只处理缺失行)。"""
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT comment_id, raw FROM comments "
                "WHERE (author_id IS NULL OR author_id='') AND raw LIKE '%username%'").fetchall()
            n = 0
            for cid, raw in rows:
                try:
                    aid = (json.loads(raw) or {}).get("username") or ""
                except Exception:
                    aid = ""
                if aid:
                    c.execute("UPDATE comments SET author_id=? WHERE comment_id=?", (aid, cid))
                    n += 1
            if n:
                c.commit()
            return n
        finally:
            c.close()

    # ---------- 本工具发出的评论 ----------
    # 链路:发出去 -> 拿到 comment_id 就登记 own_comments;
    #       拿不到 -> 记 own_comment_pending;抓到评论后用 export_id+内容补登。
    # 隐藏判据另外还有「作者身份」一路(见 _own_exclusion),登记表是溯源用的权威记录。
    def mark_own_comment(self, account_id, comment_id, via, content=None, export_id=None):
        """登记一条本工具发出的评论。

        content 可选:发出的文本。给了就顺手清掉对应的待确认登记
        (说明这条已经拿到真实 id,不需要再回填)。
        """
        if not comment_id:
            return
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO own_comments(account_id,comment_id,via,created_at) VALUES(?,?,?,?)",
                      (account_id, comment_id, via, datetime.now().isoformat()))
            if content:
                c.execute("DELETE FROM own_comment_pending WHERE account_id=? AND content=?",
                          (account_id, content))
            c.commit()
        finally:
            c.close()

    def mark_own_comment_pending(self, account_id, export_id, content, via):
        """发出去了但没拿到 comment_id:先记待确认,等抓到评论再补登。

        同一 (账号,作品,内容,来源) 只留一条,避免重试堆积。
        """
        if not content:
            return
        c = self._conn()
        try:
            dup = c.execute("""SELECT 1 FROM own_comment_pending
                               WHERE account_id=? AND COALESCE(export_id,'')=? AND content=? AND via=?""",
                            (account_id, export_id or "", content, via)).fetchone()
            if not dup:
                c.execute("INSERT INTO own_comment_pending(account_id,export_id,content,via,created_at) VALUES(?,?,?,?,?)",
                          (account_id, export_id or "", content, via, datetime.now().isoformat()))
                c.commit()
        finally:
            c.close()

    def pending_own_comment_count(self, account_id=None):
        c = self._conn()
        try:
            if account_id:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending WHERE account_id=?",
                              (account_id,)).fetchone()
            else:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending").fetchone()
            return r[0] if r else 0
        finally:
            c.close()

    def reconcile_own_comments(self, account_id=None, max_age_days=30):
        """把「当时没拿到 comment_id」的自己评论补登进 own_comments。返回补登条数。

        匹配依据:同账号 + export_id(若记录里没有则不限)+ 内容完全相同 +
                  尚未在 own_comments 里的评论。内容逐字相同且是同一作品,
        基本只可能是自己刚发的那条 —— 客户不会复述我方话术。
        补不上的保留待确认;超过 max_age_days 仍未匹配的丢弃,避免无限堆积。
        """
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT id, account_id, export_id, content, via FROM own_comment_pending"
                + (" WHERE account_id=?" if account_id else ""),
                (account_id,) if account_id else ()).fetchall()
            if not rows:
                return 0
            matched = 0
            for pid, acc, exp, content, via in rows:
                if not content:
                    c.execute("DELETE FROM own_comment_pending WHERE id=?", (pid,))
                    continue
                row = c.execute(
                    """SELECT comment_id FROM comments
                        WHERE account_id=? AND content=?
                          AND (?='' OR export_id=?)
                          AND deleted=0 AND COALESCE(comment_id,'')<>''
                          AND comment_id NOT IN
                              (SELECT comment_id FROM own_comments WHERE COALESCE(comment_id,'')<>'')
                        ORDER BY create_time ASC, id ASC LIMIT 1""",
                    (acc, content, exp or "", exp or "")).fetchone()
                if not row:
                    continue
                c.execute("INSERT OR IGNORE INTO own_comments(account_id,comment_id,via,created_at) VALUES(?,?,?,?)",
                          (acc, row[0], via, datetime.now().isoformat()))
                c.execute("DELETE FROM own_comment_pending WHERE id=?", (pid,))
                matched += 1
                logger.info("[%s] 补登自己发出的评论 %s(via=%s): %s",
                            acc, row[0], via, (content or "")[:20])
            # 超龄未匹配:丢弃(评论可能早被删/抓不到),不让待确认表无限增长
            c.execute("DELETE FROM own_comment_pending WHERE created_at < ?",
                      ((datetime.now() - timedelta(days=max_age_days)).isoformat(),))
            c.commit()
            return matched
        finally:
            c.close()
