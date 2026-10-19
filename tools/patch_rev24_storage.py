# -*- coding: utf-8 -*-
"""rev24 补丁 1/4:storage 层评论归属主键 account_id -> finder_id。

不变式(与 rev23 作品同一条逻辑):
  评论是「视频号」的资产,不是「登录卡片」的。
  finder_id = 归属主键;account_id = 最后一次抓到它的登录卡片(只做追溯)。

涉及 6 张表:
  comments / video_stats / auto_commented / own_comments /
  own_comment_pending / delete_logs

一次性整文件读写 + 逐条 assert 唯一命中(同轮多条 Edit 只有最后一条落盘的坑)。
"""
import io
import sys

P = r"D:\SPHGJ\backend\storage.py"
s = io.open(P, encoding="utf-8").read()
orig = s
PATCHES = []


def patch(tag, old, new, count=1):
    PATCHES.append((tag, old, new, count))


# ---------------------------------------------------------------- 1. 模块 docstring
patch("docstring",
      '''  comments        评论(account_id/export_id/comment_id UNIQUE/nickname/content/create_time/replied ...)
  video_stats     视频评论数(account_id+export_id 主键)--增量对比:count 没变则跳过抓取
  auto_commented  已自动评论的视频(避免重发)
  own_comments    本工具发出的评论登记(自动评论/自动回复/手动回复,隐藏与溯源用)
  own_comment_pending  发出但接口没回 comment_id 的评论,抓取时按 export_id+内容补登''',
      '''  comments        评论(finder_id/export_id/comment_id UNIQUE/nickname/content/create_time/replied ...)
  video_stats     视频评论数(finder_id+export_id 主键)--增量对比:count 没变则跳过抓取
  auto_commented  已自动评论的视频(避免重发)
  own_comments    本工具发出的评论登记(自动评论/自动回复/手动回复,隐藏与溯源用)
  own_comment_pending  发出但接口没回 comment_id 的评论,抓取时按 export_id+内容补登

归属主键 = finder_id(视频号 ID),account_id 只记「最后一次抓到它的登录卡片」。
理由同作品表:卡片删了重建 / 同视频号挂两张卡片 / 未锁定卡片换号扫码,都不该让
评论数据失联、重复或混归属。''')

# ---------------------------------------------------------------- 2. DDL 常量
patch("ddl-const",
      '''META_DDL = """CREATE TABLE IF NOT EXISTS post_fetch_meta(
            finder_id TEXT PRIMARY KEY,
            account_id TEXT,
            last_refresh TEXT, last_pages INTEGER,
            today_date TEXT, today_requests INTEGER,
            full_synced INTEGER DEFAULT 0)"""''',
      '''META_DDL = """CREATE TABLE IF NOT EXISTS post_fetch_meta(
            finder_id TEXT PRIMARY KEY,
            account_id TEXT,
            last_refresh TEXT, last_pages INTEGER,
            today_date TEXT, today_requests INTEGER,
            full_synced INTEGER DEFAULT 0)"""

# 评论域 DDL 同样抽成常量:建表与迁移重建共用,避免两处漂移。
COMMENTS_DDL = """CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT,
            account_id TEXT, export_id TEXT, comment_id TEXT UNIQUE,
            author_id TEXT,
            nickname TEXT, content TEXT, head_url TEXT, create_time INTEGER,
            like_count INTEGER, read_flag INTEGER, replied INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            raw TEXT, fetched_at TEXT)"""

VIDEO_STATS_DDL = """CREATE TABLE IF NOT EXISTS video_stats(
            finder_id TEXT, export_id TEXT, comment_count INTEGER, updated_at TEXT,
            account_id TEXT,
            PRIMARY KEY(finder_id, export_id))"""

AUTO_COMMENTED_DDL = """CREATE TABLE IF NOT EXISTS auto_commented(
            finder_id TEXT, export_id TEXT, comment_id TEXT, commented_at TEXT,
            account_id TEXT,
            PRIMARY KEY(finder_id, export_id))"""

# 只需补 finder_id 列(不必重建表)的评论域表
COMMENT_FID_TABLES = ("comments", "own_comments", "own_comment_pending", "delete_logs")''')

# ---------------------------------------------------------------- 3. 三张主表 DDL 调用
patch("ddl-main",
      '''        c.execute("""CREATE TABLE IF NOT EXISTS comments(
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
            PRIMARY KEY(account_id, export_id))""")''',
      '''        c.execute(COMMENTS_DDL)
        c.execute(VIDEO_STATS_DDL)
        c.execute(AUTO_COMMENTED_DDL)''')

# ---------------------------------------------------------------- 4. delete_logs DDL
patch("ddl-delete-logs",
      '''        c.execute("""CREATE TABLE IF NOT EXISTS delete_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, comment_id TEXT, export_id TEXT,
            nickname TEXT, content TEXT, keyword TEXT, deleted_at TEXT)""")''',
      '''        c.execute("""CREATE TABLE IF NOT EXISTS delete_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT,
            account_id TEXT, comment_id TEXT, export_id TEXT,
            nickname TEXT, content TEXT, keyword TEXT, deleted_at TEXT)""")''')

# ---------------------------------------------------------------- 5. 索引前跑结构性迁移
patch("index-migrate",
      '''        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_time ON comments(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_acc ON comments(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_time ON delete_logs(deleted_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_acc ON delete_logs(account_id)")''',
      '''        # 评论域归属主键迁移(account_id -> finder_id)。必须在建 finder 索引【之前】跑:
        # 旧库还没有 finder_id 列时 CREATE INDEX 会 "no such column" 直接把启动打挂。
        self._migrate_comment_schema(c)
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_time ON comments(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_acc ON comments(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_finder ON comments(finder_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_time ON delete_logs(deleted_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_acc ON delete_logs(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_finder ON delete_logs(finder_id)")''')

# ---------------------------------------------------------------- 6. own_comments DDL
patch("ddl-own-comments",
      '''        c.execute("""CREATE TABLE IF NOT EXISTS own_comments(
            account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT)""")''',
      '''        c.execute("""CREATE TABLE IF NOT EXISTS own_comments(
            finder_id TEXT, account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT)""")''')

# ---------------------------------------------------------------- 7. own_comment_pending DDL
patch("ddl-own-pending",
      '''        c.execute("""CREATE TABLE IF NOT EXISTS own_comment_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT, export_id TEXT, content TEXT, via TEXT, created_at TEXT)""")''',
      '''        c.execute("""CREATE TABLE IF NOT EXISTS own_comment_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT, account_id TEXT, export_id TEXT, content TEXT, via TEXT, created_at TEXT)""")''')

# ---------------------------------------------------------------- 8. 结构性迁移方法
patch("migrate-schema-method",
      '''    def upsert_comment(self, account_id, export_id, comment):''',
      '''    def _migrate_comment_schema(self, c):
        """评论域归属主键 account_id -> finder_id 的【结构】迁移。幂等,不依赖配置文件。

        为什么放在 _init:任何入口(server / 自测脚本 / 运维工具)打开库都拿到一致的
        schema,不用赌调用方先跑迁移。这里看不到配置,只能把归属填成
        "acct:<account_id>" 兜底;真实视频号由 migrate_comments_to_finder(配置映射)
        在启动时换成真值 —— 宁可先标「未知视频号」,也绝不丢数据。
        """
        # 1) 补列 + 用旧的 account_id 兜底填 finder_id
        for t in COMMENT_FID_TABLES:
            try:
                c.execute(f"ALTER TABLE {t} ADD COLUMN finder_id TEXT")
            except Exception:
                pass
            c.execute(f"UPDATE {t} SET finder_id='acct:'||account_id "
                      "WHERE COALESCE(finder_id,'')='' AND COALESCE(account_id,'')<>''")
        # 2) 主键含 account_id 的两张表只能重建(旧库升级路径)
        cols = [r[1] for r in c.execute("PRAGMA table_info(video_stats)").fetchall()]
        if cols and "finder_id" not in cols:
            c.execute("ALTER TABLE video_stats RENAME TO video_stats_old")
            c.execute(VIDEO_STATS_DDL)
            agg = {}
            for acc, eid, cc, ts in c.execute(
                    "SELECT account_id,export_id,comment_count,updated_at FROM video_stats_old").fetchall():
                k = ("acct:" + (acc or "unknown"), eid or "")
                if k not in agg or (cc or 0) > (agg[k][0] or 0):
                    agg[k] = (cc, ts, acc)
            c.executemany(
                "INSERT OR REPLACE INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id) "
                "VALUES(?,?,?,?,?)", [(k[0], k[1], v[0], v[1], v[2]) for k, v in agg.items()])
            c.execute("DROP TABLE video_stats_old")
        cols = [r[1] for r in c.execute("PRAGMA table_info(auto_commented)").fetchall()]
        if cols and "finder_id" not in cols:
            c.execute("ALTER TABLE auto_commented RENAME TO auto_commented_old")
            c.execute(AUTO_COMMENTED_DDL)
            agg = {}
            for acc, eid, cid, ts in c.execute(
                    "SELECT account_id,export_id,comment_id,commented_at FROM auto_commented_old").fetchall():
                k = ("acct:" + (acc or "unknown"), eid or "")
                if k not in agg or (ts or "") > (agg[k][1] or ""):
                    agg[k] = (cid, ts, acc)
            c.executemany(
                "INSERT OR REPLACE INTO auto_commented(finder_id,export_id,comment_id,commented_at,account_id) "
                "VALUES(?,?,?,?,?)", [(k[0], k[1], v[0], v[1], v[2]) for k, v in agg.items()])
            c.execute("DROP TABLE auto_commented_old")
        c.commit()

    def upsert_comment(self, finder_id, export_id, comment, account_id=None):''')

# ---------------------------------------------------------------- 9. upsert_comment 主体
patch("upsert-body",
      '''        cid = comment.get("commentId")
        # 作者身份:接口每条评论都带 username(作者的视频号 id),同一次响应里与评论同源,不会错位
        author_id = comment.get("username") or comment.get("commentUsername") or ""
        # INSERT OR IGNORE:新评论插入(replied=0);已存在的不动 replied
        c.execute("""INSERT OR IGNORE INTO comments
            (account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, export_id, cid, author_id, comment.get("commentNickname"),''',
      '''        cid = comment.get("commentId")
        # 作者身份:接口每条评论都带 username(作者的视频号 id),同一次响应里与评论同源,不会错位
        author_id = comment.get("username") or comment.get("commentUsername") or ""
        # INSERT OR IGNORE:新评论插入(replied=0);已存在的不动 replied
        c.execute("""INSERT OR IGNORE INTO comments
            (finder_id,account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (finder_id, account_id, export_id, cid, author_id, comment.get("commentNickname"),''')

patch("upsert-update",
      '''        # UPDATE 其他字段(不覆盖 replied)
        c.execute("""UPDATE comments SET author_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE author_id END,
                nickname=?,content=?,head_url=?,create_time=?,like_count=?,read_flag=?,raw=?,fetched_at=?
            WHERE comment_id=?""",
            (author_id, author_id, comment.get("commentNickname"), comment.get("commentContent"), comment.get("commentHeadurl"),''',
      '''        # UPDATE 其他字段(不覆盖 replied)。
        # finder_id 自愈:历史兜底值 acct:<卡片> 一律换回真实视频号 ——
        # 一条评论只可能属于一个作品,也就只可能属于一个视频号,后写的真值一定更准。
        c.execute("""UPDATE comments SET
                finder_id=CASE WHEN COALESCE(finder_id,'')='' OR finder_id LIKE 'acct:%' THEN ? ELSE finder_id END,
                account_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE account_id END,
                author_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE author_id END,
                nickname=?,content=?,head_url=?,create_time=?,like_count=?,read_flag=?,raw=?,fetched_at=?
            WHERE comment_id=?""",
            (finder_id, account_id, account_id, author_id, author_id,
             comment.get("commentNickname"), comment.get("commentContent"), comment.get("commentHeadurl"),''')

# ---------------------------------------------------------------- 10. 视频评论数快照 3 个方法
patch("video-count",
      '''    def get_video_comment_count(self, account_id, export_id):
        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()''',
      '''    def get_video_comment_count(self, finder_id, export_id):
        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()''')

patch("video-fetch-state",
      '''    def get_video_fetch_state(self, account_id, export_id):''',
      '''    def get_video_fetch_state(self, finder_id, export_id):''')

patch("video-fetch-state-body",
      '''        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        cc = r[0] if r else None
        has = c.execute("SELECT 1 FROM comments WHERE account_id=? AND export_id=? LIMIT 1",
                        (account_id, export_id)).fetchone() is not None''',
      '''        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        cc = r[0] if r else None
        has = c.execute("SELECT 1 FROM comments WHERE finder_id=? AND export_id=? LIMIT 1",
                        (finder_id, export_id)).fetchone() is not None''')

patch("set-video-count",
      '''    def set_video_comment_count(self, account_id, export_id, count):''',
      '''    def set_video_comment_count(self, finder_id, export_id, count):''')

patch("set-video-count-body",
      '''        r = c.execute("SELECT comment_count FROM video_stats WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        old = r[0] if r else None
        if old is not None and new < old:
            new = old            # 脏值:保留旧值,不回退
        c.execute("""INSERT OR REPLACE INTO video_stats(account_id,export_id,comment_count,updated_at)
            VALUES(?,?,?,?)""", (account_id, export_id, new, datetime.now().isoformat()))''',
      '''        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        old = r[0] if r else None
        if old is not None and new < old:
            new = old            # 脏值:保留旧值,不回退
        c.execute("""INSERT OR REPLACE INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id)
            VALUES(?,?,?,?,COALESCE((SELECT account_id FROM video_stats WHERE finder_id=? AND export_id=?),''))""",
                  (finder_id, export_id, new, datetime.now().isoformat(), finder_id, export_id))''')

patch("reset-video-count",
      '''    def reset_video_comment_count(self, account_id=None, export_id=None):''',
      '''    def reset_video_comment_count(self, finder_id=None, export_id=None):''')

patch("reset-video-count-body",
      '''        用于修历史脏数据:video_stats 里记了 count 但 comments 表一条都没有,
        说明该视频从未真正抓到过评论(被 0 值快照锁死),必须复位重抓。
        account_id / export_id 为 None 表示不限定。
        """
        c = self._conn()
        where, args = [], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)''',
      '''        用于修历史脏数据:video_stats 里记了 count 但 comments 表一条都没有,
        说明该视频从未真正抓到过评论(被 0 值快照锁死),必须复位重抓。
        finder_id / export_id 为 None 表示不限定。
        """
        c = self._conn()
        where, args = [], []
        if finder_id:
            where.append("finder_id=?"); args.append(finder_id)''')

# ---------------------------------------------------------------- 11. reset_stale_video_counts
patch("reset-stale",
      '''        c = self._conn()
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
                    [acc] + chunk).rowcount or 0''',
      '''        c = self._conn()
        rows = c.execute("""SELECT vs.finder_id, vs.export_id FROM video_stats vs
            WHERE vs.comment_count > 0
              AND NOT EXISTS (
                SELECT 1 FROM comments cm
                WHERE cm.finder_id = vs.finder_id AND cm.export_id = vs.export_id)""").fetchall()
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
                    f"DELETE FROM video_stats WHERE finder_id=? AND export_id IN ({qs})",
                    [acc] + chunk).rowcount or 0''')

# ---------------------------------------------------------------- 12. video_fetch_health
patch("video-health",
      '''        rows = c.execute("""SELECT vs.account_id,
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
                     never_fetched=r[3] or 0, last_scan=r[4]) for r in rows]''',
      '''        rows = c.execute("""SELECT vs.finder_id, MAX(vs.account_id) AS account_id,
                COUNT(*) AS videos,
                SUM(CASE WHEN vs.comment_count > 0 THEN 1 ELSE 0 END) AS with_cc,
                SUM(CASE WHEN vs.comment_count > 0 AND NOT EXISTS (
                        SELECT 1 FROM comments cm
                        WHERE cm.finder_id = vs.finder_id AND cm.export_id = vs.export_id)
                    THEN 1 ELSE 0 END) AS never_fetched,
                MAX(vs.updated_at) AS last_scan
            FROM video_stats vs GROUP BY vs.finder_id""").fetchall()
        c.close()
        return [dict(finder_id=r[0], account_id=r[1] or "", videos=r[2] or 0, with_cc=r[3] or 0,
                     never_fetched=r[4] or 0, last_scan=r[5]) for r in rows]''')

# ---------------------------------------------------------------- 13. get_comment
patch("get-comment",
      '''            r = c.execute(
                "SELECT account_id,export_id,nickname,content FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            return dict(account_id=r[0], export_id=r[1], nickname=r[2], content=r[3])''',
      '''            r = c.execute(
                "SELECT finder_id,account_id,export_id,nickname,content FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            return dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2],
                        nickname=r[3], content=r[4])''')

# ---------------------------------------------------------------- 14. auto_commented
patch("auto-commented",
      '''    def is_auto_commented(self, account_id, export_id):
        c = self._conn()
        r = c.execute("SELECT 1 FROM auto_commented WHERE account_id=? AND export_id=?",
                      (account_id, export_id)).fetchone()
        c.close()
        return r is not None

    def set_auto_commented(self, account_id, export_id, comment_id):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO auto_commented(account_id,export_id,comment_id,commented_at) VALUES(?,?,?,?)",
                  (account_id, export_id, comment_id, datetime.now().isoformat()))''',
      '''    def is_auto_commented(self, finder_id, export_id):
        c = self._conn()
        r = c.execute("SELECT 1 FROM auto_commented WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        c.close()
        return r is not None

    def set_auto_commented(self, finder_id, export_id, comment_id, account_id=None):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO auto_commented(finder_id,export_id,comment_id,commented_at,account_id) "
                  "VALUES(?,?,?,?,?)",
                  (finder_id, export_id, comment_id, datetime.now().isoformat(), account_id))''')

# ---------------------------------------------------------------- 15. recent_comments(导出用)
patch("recent-comments",
      '''    def recent_comments(self, account_id=None, limit=200):
        """旧接口:最近评论(元组)。保留给导出/兼容。"""
        c = self._conn()
        cols = "account_id,export_id,comment_id,nickname,content,head_url,create_time,like_count,replied"
        if account_id:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE account_id=? AND deleted=0 ORDER BY create_time DESC LIMIT ?",
                (account_id, limit)).fetchall()''',
      '''    def recent_comments(self, finder_id=None, account_id=None, limit=200):
        """旧接口:最近评论(元组)。保留给导出/兼容。归属筛选优先 finder_id。"""
        c = self._conn()
        cols = ("finder_id,account_id,export_id,comment_id,nickname,content,head_url,"
                "create_time,like_count,replied")
        if finder_id:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE finder_id=? AND deleted=0 ORDER BY create_time DESC LIMIT ?",
                (finder_id, limit)).fetchall()
        elif account_id:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE account_id=? AND deleted=0 ORDER BY create_time DESC LIMIT ?",
                (account_id, limit)).fetchall()''')

# ---------------------------------------------------------------- 16. query_comments
patch("query-comments",
      '''    def query_comments(self, account_id=None, replied=None, q=None, limit=200, offset=0,
                       hide_own=False, own_map=None):''',
      '''    def query_comments(self, finder_id=None, account_id=None, replied=None, q=None, limit=200, offset=0,
                       hide_own=False, own_map=None):''')

patch("query-comments-doc",
      '''        hide_own=True 隐藏「自己发出的评论」,判据(并集,见 _own_exclusion):''',
      '''        归属筛选优先 finder_id(视频号);account_id 保留给旧前端与按卡片排查。
        hide_own=True 隐藏「自己发出的评论」,判据(并集,见 _own_exclusion):''')

patch("query-comments-body",
      '''        where, args = ["deleted=0"], []
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if replied is True:''',
      '''        where, args = ["deleted=0"], []
        if finder_id:
            where.append("finder_id=?"); args.append(finder_id)
        if account_id:
            where.append("account_id=?"); args.append(account_id)
        if replied is True:''')

patch("query-comments-select",
      '''        rows = c.execute(
            f"""SELECT account_id,export_id,comment_id,nickname,content,head_url,
                      create_time,like_count,replied,fetched_at
               FROM comments{clause}
               ORDER BY create_time DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        c.close()
        items = [dict(account_id=r[0], export_id=r[1], comment_id=r[2], nickname=r[3],
                      content=r[4], head_url=r[5], create_time=r[6], like_count=r[7],
                      replied=bool(r[8]), fetched_at=r[9]) for r in rows]''',
      '''        rows = c.execute(
            f"""SELECT finder_id,account_id,export_id,comment_id,nickname,content,head_url,
                      create_time,like_count,replied,fetched_at
               FROM comments{clause}
               ORDER BY create_time DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        c.close()
        items = [dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2], comment_id=r[3],
                      nickname=r[4], content=r[5], head_url=r[6], create_time=r[7], like_count=r[8],
                      replied=bool(r[9]), fetched_at=r[10]) for r in rows]''')

# ---------------------------------------------------------------- 17. 评论归属视图
patch("comment-finders",
      '''    def account_stats(self):''',
      '''    def comment_finders(self):
        """有评论的视频号清单(评论归属视图):评论数、最后抓取时间、来源卡片。"""
        c = self._conn()
        try:
            rows = c.execute("""SELECT finder_id, COUNT(*) AS comments,
                        MAX(account_id) AS account_id, MAX(fetched_at) AS last_fetched
                    FROM comments WHERE deleted=0 GROUP BY finder_id
                    ORDER BY comments DESC""").fetchall()
            return [dict(finder_id=r[0] or "", comments=r[1] or 0,
                         account_id=r[2] or "", last_fetched=r[3]) for r in rows]
        finally:
            c.close()

    def account_stats(self):''')

# ---------------------------------------------------------------- 18. _own_exclusion 改按视频号
patch("own-exclusion",
      '''                # 注意参数顺序必须与 SQL 里占位符出现的顺序一致:account_id 在最前
                parts.append("(account_id=? AND (%s))" % " OR ".join(cond))
                args.append(acc)''',
      '''                # 注意参数顺序必须与 SQL 里占位符出现的顺序一致:finder_id 在最前
                parts.append("(finder_id=? AND (%s))" % " OR ".join(cond))
                args.append(acc)''')

patch("own-exclusion-doc",
      '''        判据二:作者身份与本账号自己一致 —— 优先视频号 id(权威,作者改名也不受影响),''',
      '''        判据二:作者身份与本视频号自己一致 —— 优先视频号 id(权威,作者改名也不受影响),''')

# ---------------------------------------------------------------- 19. own_identities_from_library 按视频号
patch("own-identities",
      '''                rows = c.execute(
                    f"""SELECT account_id, {col}, COUNT(*) FROM comments
                        WHERE deleted=0 AND COALESCE({col},'')<>''
                        GROUP BY account_id, {col}""").fetchall()''',
      '''                rows = c.execute(
                    f"""SELECT finder_id, {col}, COUNT(*) FROM comments
                        WHERE deleted=0 AND COALESCE({col},'')<>''
                        GROUP BY finder_id, {col}""").fetchall()''')

# ---------------------------------------------------------------- 20. log_delete
patch("log-delete",
      '''    def log_delete(self, account_id, comment_id, nickname, content, keyword, export_id):
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (account_id,comment_id,export_id,nickname,content,keyword,deleted_at)
                VALUES(?,?,?,?,?,?,?)""",
                      (account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat()))''',
      '''    def log_delete(self, finder_id, comment_id, nickname, content, keyword, export_id,
                   account_id=None):
        """写删除记录。归属 = finder_id(视频号),account_id 只记执行删除的卡片。"""
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                      (finder_id, account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat()))''')

# ---------------------------------------------------------------- 21. query_delete_logs
patch("query-delete-logs",
      '''    def query_delete_logs(self, account_id=None, q=None, limit=200, offset=0):
        """分页查询删除记录,返回 (list[dict], total)。q 匹配 content/nickname/keyword。"""
        c = self._conn()
        try:
            where, args = [], []
            if account_id:
                where.append("account_id=?"); args.append(account_id)''',
      '''    def query_delete_logs(self, finder_id=None, account_id=None, q=None, limit=200, offset=0):
        """分页查询删除记录,返回 (list[dict], total)。q 匹配 content/nickname/keyword。

        归属筛选优先 finder_id(视频号);account_id 留给按卡片排查历史记录。
        """
        c = self._conn()
        try:
            where, args = [], []
            if finder_id:
                where.append("finder_id=?"); args.append(finder_id)
            if account_id:
                where.append("account_id=?"); args.append(account_id)''')

patch("query-delete-logs-select",
      '''            rows = c.execute(
                f"""SELECT account_id,comment_id,export_id,nickname,content,keyword,deleted_at
                    FROM delete_logs{clause}
                    ORDER BY deleted_at DESC LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(account_id=r[0], comment_id=r[1], export_id=r[2], nickname=r[3],
                          content=r[4], keyword=r[5], deleted_at=r[6]) for r in rows]''',
      '''            rows = c.execute(
                f"""SELECT finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at
                    FROM delete_logs{clause}
                    ORDER BY deleted_at DESC LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(finder_id=r[0] or "", account_id=r[1], comment_id=r[2], export_id=r[3],
                          nickname=r[4], content=r[5], keyword=r[6], deleted_at=r[7]) for r in rows]''')

# ---------------------------------------------------------------- 22. clear_delete_logs
patch("clear-delete-logs",
      '''    def clear_delete_logs(self, account_id=None):
        c = self._conn()
        try:
            if account_id:
                c.execute("DELETE FROM delete_logs WHERE account_id=?", (account_id,))
            else:
                c.execute("DELETE FROM delete_logs")''',
      '''    def clear_delete_logs(self, finder_id=None, account_id=None):
        c = self._conn()
        try:
            if finder_id:
                c.execute("DELETE FROM delete_logs WHERE finder_id=?", (finder_id,))
            elif account_id:
                c.execute("DELETE FROM delete_logs WHERE account_id=?", (account_id,))
            else:
                c.execute("DELETE FROM delete_logs")''')

# ---------------------------------------------------------------- 23. mark_own_comment
patch("mark-own-comment",
      '''    def mark_own_comment(self, account_id, comment_id, via, content=None, export_id=None):
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
                          (account_id, content))''',
      '''    def mark_own_comment(self, finder_id, comment_id, via, content=None, export_id=None,
                         account_id=None):
        """登记一条本工具发出的评论。归属 = finder_id(视频号)。

        content 可选:发出的文本。给了就顺手清掉对应的待确认登记
        (说明这条已经拿到真实 id,不需要再回填)。
        """
        if not comment_id:
            return
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO own_comments(finder_id,account_id,comment_id,via,created_at) "
                      "VALUES(?,?,?,?,?)",
                      (finder_id, account_id, comment_id, via, datetime.now().isoformat()))
            if content:
                c.execute("DELETE FROM own_comment_pending WHERE finder_id=? AND content=?",
                          (finder_id, content))''')

# ---------------------------------------------------------------- 24. mark_own_comment_pending
patch("mark-own-pending",
      '''    def mark_own_comment_pending(self, account_id, export_id, content, via):
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
                          (account_id, export_id or "", content, via, datetime.now().isoformat()))''',
      '''    def mark_own_comment_pending(self, finder_id, export_id, content, via, account_id=None):
        """发出去了但没拿到 comment_id:先记待确认,等抓到评论再补登。

        同一 (视频号,作品,内容,来源) 只留一条,避免重试堆积。
        """
        if not content:
            return
        c = self._conn()
        try:
            dup = c.execute("""SELECT 1 FROM own_comment_pending
                               WHERE finder_id=? AND COALESCE(export_id,'')=? AND content=? AND via=?""",
                            (finder_id, export_id or "", content, via)).fetchone()
            if not dup:
                c.execute("INSERT INTO own_comment_pending(finder_id,account_id,export_id,content,via,created_at) "
                          "VALUES(?,?,?,?,?,?)",
                          (finder_id, account_id, export_id or "", content, via, datetime.now().isoformat()))''')

# ---------------------------------------------------------------- 25. pending_own_comment_count
patch("pending-count",
      '''    def pending_own_comment_count(self, account_id=None):
        c = self._conn()
        try:
            if account_id:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending WHERE account_id=?",
                              (account_id,)).fetchone()''',
      '''    def pending_own_comment_count(self, finder_id=None):
        c = self._conn()
        try:
            if finder_id:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending WHERE finder_id=?",
                              (finder_id,)).fetchone()''')

# ---------------------------------------------------------------- 26. reconcile_own_comments
patch("reconcile",
      '''    def reconcile_own_comments(self, account_id=None, max_age_days=30):
        """把「当时没拿到 comment_id」的自己评论补登进 own_comments。返回补登条数。

        匹配依据:同账号 + export_id(若记录里没有则不限)+ 内容完全相同 +''',
      '''    def reconcile_own_comments(self, finder_id=None, max_age_days=30):
        """把「当时没拿到 comment_id」的自己评论补登进 own_comments。返回补登条数。

        匹配依据:同视频号 + export_id(若记录里没有则不限)+ 内容完全相同 +''')

patch("reconcile-body",
      '''            rows = c.execute(
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
                        WHERE account_id=? AND content=?''',
      '''            rows = c.execute(
                "SELECT id, finder_id, account_id, export_id, content, via FROM own_comment_pending"
                + (" WHERE finder_id=?" if finder_id else ""),
                (finder_id,) if finder_id else ()).fetchall()
            if not rows:
                return 0
            matched = 0
            for pid, fid, acc, exp, content, via in rows:
                if not content:
                    c.execute("DELETE FROM own_comment_pending WHERE id=?", (pid,))
                    continue
                row = c.execute(
                    """SELECT comment_id FROM comments
                        WHERE finder_id=? AND content=?''')

patch("reconcile-insert",
      '''                        ORDER BY create_time ASC, id ASC LIMIT 1""",
                    (acc, content, exp or "", exp or "")).fetchone()
                if not row:
                    continue
                c.execute("INSERT OR IGNORE INTO own_comments(account_id,comment_id,via,created_at) VALUES(?,?,?,?)",
                          (acc, row[0], via, datetime.now().isoformat()))''',
      '''                        ORDER BY create_time ASC, id ASC LIMIT 1""",
                    (fid, content, exp or "", exp or "")).fetchone()
                if not row:
                    continue
                c.execute("INSERT OR IGNORE INTO own_comments(finder_id,account_id,comment_id,via,created_at) "
                          "VALUES(?,?,?,?,?)",
                          (fid, acc, row[0], via, datetime.now().isoformat()))''')

patch("reconcile-log",
      '''                logger.info("[%s] 补登自己发出的评论 %s(via=%s): %s",
                            acc, row[0], via, (content or "")[:20])''',
      '''                logger.info("[%s] 补登自己发出的评论 %s(via=%s): %s",
                            fid, row[0], via, (content or "")[:20])''')

# ---------------------------------------------------------------- 27. migrate_comments_to_finder
patch("migrate-to-finder",
      '''    @staticmethod
    def _own_exclusion(own_map=None):''',
      '''    def migrate_comments_to_finder(self, fid_map=None):
        """把评论域归属从兜底 "acct:<卡片>" 换成真实视频号 finder_id。幂等。

        _init 的结构迁移看不到配置,只能填 acct: 兜底;这里用
        {account_id: finder_id} 换成真值,并把「同一视频号挂过两张卡片」产生的
        重复行合并掉:
          - comments / own_comments / delete_logs 靠 comment_id 天然唯一,直改即可;
          - video_stats / auto_commented 旧主键含 account_id,需按 (finder_id, export_id)
            去重合并(保留评论数更大 / 记录更新的那条)。
        返回 dict(tables={表名:改动行数}, merged=合并掉的重复行数)。
        """
        fid_map = {k: (v or "").strip() for k, v in (fid_map or {}).items() if k and v}
        res = {"tables": {}, "merged": 0}
        if not fid_map:
            return res
        c = self._conn()
        try:
            for t in COMMENT_FID_TABLES:
                n = 0
                for acc, fid in fid_map.items():
                    cur = c.execute(
                        f"UPDATE {t} SET finder_id=? WHERE account_id=? "
                        "AND (COALESCE(finder_id,'')='' OR finder_id=?)",
                        (fid, acc, "acct:" + acc))
                    n += cur.rowcount or 0
                res["tables"][t] = n
            res["tables"]["video_stats"] = self._dedup_by_finder(c, "video_stats", fid_map, res)
            res["tables"]["auto_commented"] = self._dedup_by_finder(c, "auto_commented", fid_map, res)
            c.commit()
            return res
        finally:
            c.close()

    @staticmethod
    def _dedup_by_finder(c, table, fid_map, res):
        """把 <table> 的 finder_id 兜底值换成真值,并按 (finder_id, export_id) 去重。"""
        if table == "video_stats":
            rows = c.execute("SELECT finder_id,account_id,export_id,comment_count,updated_at "
                             "FROM video_stats").fetchall()
        else:
            rows = c.execute("SELECT finder_id,account_id,export_id,commented_at,comment_id "
                             "FROM auto_commented").fetchall()
        groups, need = {}, False
        for fid, acc, eid, a, b in rows:
            fid2 = fid or ""
            if not fid2 or fid2.startswith("acct:"):
                fid2 = fid_map.get(acc or "") or fid2 or ("acct:" + (acc or "unknown"))
                need = True
            groups.setdefault((fid2, eid or ""), []).append((a, b, acc))
        merged = sum(len(v) - 1 for v in groups.values() if len(v) > 1)
        if not need and not merged:
            return 0
        c.execute(f"DELETE FROM {table}")
        if table == "video_stats":
            keep = {k: max(v, key=lambda x: (x[0] or 0)) for k, v in groups.items()}
            c.executemany(
                "INSERT OR REPLACE INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id) "
                "VALUES(?,?,?,?,?)", [(f, e, v[0], v[1], v[2]) for (f, e), v in keep.items()])
        else:
            keep = {k: max(v, key=lambda x: (x[0] or "")) for k, v in groups.items()}
            c.executemany(
                "INSERT OR REPLACE INTO auto_commented(finder_id,export_id,comment_id,commented_at,account_id) "
                "VALUES(?,?,?,?,?)", [(f, e, v[1], v[0], v[2]) for (f, e), v in keep.items()])
        res["merged"] += merged
        return len(rows)

    @staticmethod
    def _own_exclusion(own_map=None):''')

# ================================================================ apply
fail = []
for tag, old, new, cnt in PATCHES:
    n = s.count(old)
    if n != cnt:
        fail.append(f"{tag}: 命中 {n} 次(期望 {cnt})")
        continue
    s = s.replace(old, new, cnt)
if fail:
    print("PATCH FAIL")
    for f in fail:
        print("  -", f)
    sys.exit(1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ================================================================ selfcheck
s2 = io.open(P, encoding="utf-8").read()
checks = [
    ("COMMENTS_DDL" in s2, "DDL 常量"),
    ("self._migrate_comment_schema(c)" in s2, "结构性迁移调用"),
    ("def migrate_comments_to_finder" in s2, "配置映射迁移"),
    ("def _dedup_by_finder" in s2, "去重合并"),
    ("def upsert_comment(self, finder_id, export_id, comment, account_id=None)" in s2, "upsert 签名"),
    ("SELECT comment_count FROM video_stats WHERE finder_id=?" in s2, "video_stats 读"),
    ("INSERT OR REPLACE INTO video_stats(finder_id" in s2, "video_stats 写"),
    ("SELECT 1 FROM auto_commented WHERE finder_id=?" in s2, "auto_commented 读"),
    ("def log_delete(self, finder_id" in s2, "log_delete 签名"),
    ("def mark_own_comment(self, finder_id" in s2, "mark_own_comment 签名"),
    ("def mark_own_comment_pending(self, finder_id" in s2, "pending 签名"),
    ("def reconcile_own_comments(self, finder_id=None" in s2, "补登签名"),
    ("WHERE cm.finder_id = vs.finder_id" in s2, "脏快照判据"),
    ('parts.append("(finder_id=? AND (%s))"' in s2, "隐藏自己评论判据"),
    ("GROUP BY finder_id, {col}" in s2, "身份统计分组"),
    ("def comment_finders" in s2, "归属视图"),
    ("idx_comments_finder" in s2 and "idx_delete_logs_finder" in s2, "finder 索引"),
]
bad = [t for ok, t in checks if not ok]
print("SELFCHECK", "PASS" if not bad else "FAIL: " + ", ".join(bad))
print("bytes", len(orig), "->", len(s2))
sys.exit(0 if not bad else 2)
