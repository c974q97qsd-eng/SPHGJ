"""SQLite 存储:评论 + 视频评论数(增量对比用)+ 自动评论记录。

表:
  comments        评论(finder_id/export_id/comment_id UNIQUE/nickname/content/create_time/replied ...)
  video_stats     视频评论数(finder_id+export_id 主键)--增量对比:count 没变则跳过抓取
  auto_commented  已自动评论的视频(避免重发)
  own_comments    本工具发出的评论登记(自动评论/自动回复/手动回复,隐藏与溯源用)
  own_comment_pending  发出但接口没回 comment_id 的评论,抓取时按 export_id+内容补登

归属主键 = finder_id(视频号 ID),account_id 只记「最后一次抓到它的登录卡片」。
理由同作品表:卡片删了重建 / 同视频号挂两张卡片 / 未锁定卡片换号扫码,都不该让
评论数据失联、重复或混归属。
"""
import logging
import os
import json
import re
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger("sphgj")

# 作品归属主键 = finder_id(视频号 ID)。DDL 抽成常量,_init 建表与迁移重建共用,避免漂移。
POSTS_DDL = """CREATE TABLE IF NOT EXISTS posts(
            finder_id TEXT, object_id TEXT,
            account_id TEXT,
            export_id TEXT, title TEXT, cover_url TEXT, cover_hash TEXT, cover_path TEXT,
            create_time INTEGER, read_count INTEGER, like_count INTEGER, comment_count INTEGER,
            forward_count INTEGER, fav_count INTEGER, follow_count INTEGER,
            visible_type INTEGER, sticky_op INTEGER, stat_sig TEXT,
            raw TEXT, updated_at TEXT,
            PRIMARY KEY(finder_id, object_id))"""

META_DDL = """CREATE TABLE IF NOT EXISTS post_fetch_meta(
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
COMMENT_FID_TABLES = ("comments", "own_comments", "own_comment_pending", "delete_logs")


def cover_key(finder_id):
    """封面子目录名:按视频号 finder_id 取目录(仅替换 Windows 非法字符)。

    未识别的历史账号用 "acct:<id>" 兜底,目录名转成 "acct_<id>"(冒号不能做目录名)。
    """
    fid = (finder_id or "").strip()
    if not fid:
        return "unknown"
    if fid.startswith("acct:"):
        fid = "acct_" + fid[5:]
    return re.sub(r"[^0-9A-Za-z_@.\-]", "_", fid)


def _move_cover_dirs(root, acc2fid):
    """covers/<account_id>/ -> covers/<finder_key>/(同卷 rename;目标已存在则逐文件并入)。"""
    if not root or not os.path.isdir(root):
        return 0
    n = 0
    for acc, fid in acc2fid.items():
        src = os.path.join(root, acc)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(root, cover_key(fid))
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        try:
            if not os.path.exists(dst):
                os.replace(src, dst)
                n += 1
                continue
            for fn in os.listdir(src):
                sp, dp = os.path.join(src, fn), os.path.join(dst, fn)
                if os.path.isfile(sp) and not os.path.exists(dp):
                    os.replace(sp, dp)
            try:
                os.rmdir(src)
            except OSError:
                pass
            n += 1
        except Exception as e:
            logger.warning(f"封面目录迁移失败 {src} -> {dst}: {e}")
    return n


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
        c.execute(COMMENTS_DDL)
        c.execute(VIDEO_STATS_DDL)
        c.execute(AUTO_COMMENTED_DDL)
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
            finder_id TEXT,
            account_id TEXT, comment_id TEXT, export_id TEXT,
            nickname TEXT, content TEXT, keyword TEXT, deleted_at TEXT)""")
        # 评论域归属主键迁移(account_id -> finder_id)。必须在建 finder 索引【之前】跑:
        # 旧库还没有 finder_id 列时 CREATE INDEX 会 "no such column" 直接把启动打挂。
        self._migrate_comment_schema(c)
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_time ON comments(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_acc ON comments(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_finder ON comments(finder_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_time ON delete_logs(deleted_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_acc ON delete_logs(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_delete_logs_finder ON delete_logs(finder_id)")
        # 作品管理:作品本地库 + 抓取账本 + 本工具发出的评论(自动回复/自动评论/手动回复)
        # 归属主键 = finder_id(视频号 ID);account_id 只记「最后抓取它的登录卡片」,不参与主键。
        c.execute(POSTS_DDL)
        c.execute(META_DDL)
        c.execute("""CREATE TABLE IF NOT EXISTS own_comments(
            finder_id TEXT, account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT)""")
        # 待确认登记:发评论/回复成功但接口没回 comment_id 时先记下,
        # 下一轮抓到该评论(comment_list 会带回自己发的评论)再补登真实 id
        c.execute("""CREATE TABLE IF NOT EXISTS own_comment_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT, account_id TEXT, export_id TEXT, content TEXT, via TEXT, created_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_own_pending_acc ON own_comment_pending(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
        # 旧库(以 account_id 为主键)还没有 finder_id 列,索引交给 migrate_posts_to_finder 重建,
        # 否则这里会 "no such column: finder_id" 直接把启动打挂
        if "finder_id" in [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]:
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_finder ON posts(finder_id)")
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
        # 自动发作品:标题池 + 发布队列(归属一律 finder_id,与作品/评论同口径)
        c.execute("""CREATE TABLE IF NOT EXISTS publish_titles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT DEFAULT '',
            content TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            used_task_id INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS publish_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT,
            account_id TEXT,
            src_path TEXT,
            file_name TEXT,
            file_size INTEGER DEFAULT 0,
            file_fp TEXT,
            title TEXT,
            title_id INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            step TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            object_id TEXT DEFAULT '',
            export_id TEXT DEFAULT '',
            queued_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            archived_name TEXT DEFAULT '',
            UNIQUE(finder_id, file_fp))""")
        # 老库补列(rev26):归档后的文件名,便于回溯「这条发的是哪个文件」
        _cols = {r[1] for r in c.execute("PRAGMA table_info(publish_tasks)")}
        if "archived_name" not in _cols:
            c.execute("ALTER TABLE publish_tasks ADD COLUMN archived_name TEXT DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_tasks_status ON publish_tasks(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_tasks_finder ON publish_tasks(finder_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_titles_finder ON publish_titles(finder_id)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀
        c.commit()
        c.close()
        # 历史评论 raw 里本就带作者 id,补一次(只处理缺列的行)
        try:
            self._backfill_author_ids()
        except Exception:
            pass

    def _migrate_comment_schema(self, c):
        """评论域归属主键 account_id -> finder_id 的【结构】迁移。幂等,不依赖配置文件。

        为什么放在 _init:任何入口(server / 自测脚本 / 运维工具)打开库都拿到一致的
        schema,不用赌调用方先跑迁移。这里看不到配置,只能把归属填成
        "acct:<account_id>" 兜底;真实视频号由 migrate_comments_to_finder(配置映射)
        在启动时换成真值 —— 宁可先标「未知视频号」,也绝不丢数据。
        """
        # 1) 补列 + 用旧的 account_id 兜底填 finder_id
        # 注意:全新库此时表还没建(本函数在 _init 里跑在建表之前),必须先存在才迁,
        # 否则 "no such table" 直接把初始化打挂(空库无法启动)
        existing = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in COMMENT_FID_TABLES:
            if t not in existing:
                continue
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

    def upsert_comment(self, finder_id, export_id, comment, account_id=None):
        c = self._conn()
        cid = comment.get("commentId")
        # 作者身份:接口每条评论都带 username(作者的视频号 id),同一次响应里与评论同源,不会错位
        author_id = comment.get("username") or comment.get("commentUsername") or ""
        # INSERT OR IGNORE:新评论插入(replied=0);已存在的不动 replied
        c.execute("""INSERT OR IGNORE INTO comments
            (finder_id,account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (finder_id, account_id, export_id, cid, author_id, comment.get("commentNickname"),
             comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0),
             int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, 0,
             json.dumps(comment, ensure_ascii=False), datetime.now().isoformat()))
        # UPDATE 其他字段(不覆盖 replied)。
        # finder_id 自愈:历史兜底值 acct:<卡片> 一律换回真实视频号 ——
        # 一条评论只可能属于一个作品,也就只可能属于一个视频号,后写的真值一定更准。
        c.execute("""UPDATE comments SET
                finder_id=CASE WHEN COALESCE(finder_id,'')='' OR finder_id LIKE 'acct:%' THEN ? ELSE finder_id END,
                account_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE account_id END,
                author_id=CASE WHEN COALESCE(?,'')<>'' THEN ? ELSE author_id END,
                nickname=?,content=?,head_url=?,create_time=?,like_count=?,read_flag=?,raw=?,fetched_at=?
            WHERE comment_id=?""",
            (finder_id, account_id, account_id, author_id, author_id,
             comment.get("commentNickname"), comment.get("commentContent"), comment.get("commentHeadurl"),
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

    def get_video_comment_count(self, finder_id, export_id):
        c = self._conn()
        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        c.close()
        return r[0] if r else None

    def get_video_fetch_state(self, finder_id, export_id):
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
        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        cc = r[0] if r else None
        has = c.execute("SELECT 1 FROM comments WHERE finder_id=? AND export_id=? LIMIT 1",
                        (finder_id, export_id)).fetchone() is not None
        c.close()
        return dict(seen=r is not None, cc=cc, has_comments=has)

    def set_video_comment_count(self, finder_id, export_id, count, account_id=None):
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
        r = c.execute("SELECT comment_count FROM video_stats WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        old = r[0] if r else None
        if old is not None and new < old:
            new = old            # 脏值:保留旧值,不回退
        # ON CONFLICT DO UPDATE(不用 INSERT OR REPLACE):REPLACE 是整行删除再插入,
        # 会把不参与本次写入的列(如 account_id)清空。
        c.execute("""INSERT INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id)
            VALUES(?,?,?,?,?)
            ON CONFLICT(finder_id,export_id) DO UPDATE SET
                comment_count=excluded.comment_count,
                updated_at=excluded.updated_at,
                account_id=COALESCE(NULLIF(excluded.account_id,''), account_id)""",
                  (finder_id, export_id, new, datetime.now().isoformat(), account_id or ""))
        c.commit()
        c.close()
        return new

    def reset_video_comment_count(self, finder_id=None, export_id=None):
        """清掉视频的评论数快照,强制下一轮重新抓取。返回清除条数。

        用于修历史脏数据:video_stats 里记了 count 但 comments 表一条都没有,
        说明该视频从未真正抓到过评论(被 0 值快照锁死),必须复位重抓。
        finder_id / export_id 为 None 表示不限定。
        """
        c = self._conn()
        where, args = [], []
        if finder_id:
            where.append("finder_id=?"); args.append(finder_id)
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
        rows = c.execute("""SELECT vs.finder_id, MAX(vs.account_id) AS account_id,
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
                     never_fetched=r[4] or 0, last_scan=r[5]) for r in rows]

    def get_comment(self, comment_id):
        """取单条评论(删除前用于补充删除记录)。返回 dict 或 None。"""
        c = self._conn()
        try:
            r = c.execute(
                "SELECT finder_id,account_id,export_id,nickname,content FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            return dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2],
                        nickname=r[3], content=r[4])
        finally:
            c.close()

    def delete_comment(self, comment_id):
        c = self._conn()
        c.execute("DELETE FROM comments WHERE comment_id=?", (comment_id,))
        c.commit()
        c.close()

    def is_auto_commented(self, finder_id, export_id):
        c = self._conn()
        r = c.execute("SELECT 1 FROM auto_commented WHERE finder_id=? AND export_id=?",
                      (finder_id, export_id)).fetchone()
        c.close()
        return r is not None

    def set_auto_commented(self, finder_id, export_id, comment_id, account_id=None):
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO auto_commented(finder_id,export_id,comment_id,commented_at,account_id) "
                  "VALUES(?,?,?,?,?)",
                  (finder_id, export_id, comment_id, datetime.now().isoformat(), account_id))
        c.commit()
        c.close()

    def recent_comments(self, finder_id=None, account_id=None, limit=200):
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
                (account_id, limit)).fetchall()
        else:
            rows = c.execute(
                f"SELECT {cols} FROM comments WHERE deleted=0 ORDER BY create_time DESC LIMIT ?",
                (limit,)).fetchall()
        c.close()
        return rows

    # ---------- 新增:分页查询 + 账号统计(供 API 层) ----------
    def query_comments(self, finder_id=None, account_id=None, replied=None, q=None, limit=200, offset=0,
                       hide_own=False, own_map=None):
        """分页查询评论,返回 (list[dict], total)。

        归属筛选优先 finder_id(视频号);account_id 保留给旧前端与按卡片排查。
        hide_own=True 隐藏「自己发出的评论」,判据(并集,见 _own_exclusion):
          1) comment_id 在本工具登记表(自动评论+置顶 / 自动回复 / 手动回复)
          2) 作者就是本账号自己 —— 接口给的作者视频号 id 命中 own_map[acc]['ids'],
             或昵称命中 ['names'](账号改名/旧数据 raw 缺 id 时兜底)
        """
        c = self._conn()
        where, args = ["deleted=0"], []
        if finder_id:
            where.append("finder_id=?"); args.append(finder_id)
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
            f"""SELECT finder_id,account_id,export_id,comment_id,nickname,content,head_url,
                      create_time,like_count,replied,fetched_at
               FROM comments{clause}
               ORDER BY create_time DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        c.close()
        items = [dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2], comment_id=r[3],
                      nickname=r[4], content=r[5], head_url=r[6], create_time=r[7], like_count=r[8],
                      replied=bool(r[9]), fetched_at=r[10]) for r in rows]
        return items, total

    def comment_finders(self):
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
    def log_delete(self, finder_id, comment_id, nickname, content, keyword, export_id,
                   account_id=None):
        """写删除记录。归属 = finder_id(视频号),account_id 只记执行删除的卡片。"""
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                      (finder_id, account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat()))
            c.commit()
        finally:
            c.close()

    def query_delete_logs(self, finder_id=None, account_id=None, q=None, limit=200, offset=0):
        """分页查询删除记录,返回 (list[dict], total)。q 匹配 content/nickname/keyword。

        归属筛选优先 finder_id(视频号);account_id 留给按卡片排查历史记录。
        """
        c = self._conn()
        try:
            where, args = [], []
            if finder_id:
                where.append("finder_id=?"); args.append(finder_id)
            if account_id:
                where.append("account_id=?"); args.append(account_id)
            if q:
                where.append("(content LIKE ? OR nickname LIKE ? OR keyword LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            total = c.execute(f"SELECT COUNT(*) FROM delete_logs{clause}", args).fetchone()[0]
            rows = c.execute(
                f"""SELECT finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at
                    FROM delete_logs{clause}
                    ORDER BY deleted_at DESC LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(finder_id=r[0] or "", account_id=r[1], comment_id=r[2], export_id=r[3],
                          nickname=r[4], content=r[5], keyword=r[6], deleted_at=r[7]) for r in rows]
            return items, total
        finally:
            c.close()

    def clear_delete_logs(self, finder_id=None, account_id=None):
        c = self._conn()
        try:
            if finder_id:
                c.execute("DELETE FROM delete_logs WHERE finder_id=?", (finder_id,))
            elif account_id:
                c.execute("DELETE FROM delete_logs WHERE account_id=?", (account_id,))
            else:
                c.execute("DELETE FROM delete_logs")
            c.commit()
        finally:
            c.close()

    # ==================== 作品管理 ====================
    # 归属主键 = finder_id(视频号 ID),不是登录账号卡片 id。2026-10-20 修:
    #   1) 卡片删了重建 -> account_id 变了 => 作品全部失联,还要全量重抓 2500+ 条;
    #   2) 同一个视频号挂两张卡片 -> 同一批作品按卡片各存一份,重复入库、封面重复下载;
    #   3) 未锁定的卡片允许换号扫码 -> 两个视频号的作品混在同一个 account_id 下,
    #      事后无法区分哪条作品属于哪个视频号(作品是视频号的资产,不是卡片的)。
    # account_id 降级为「最后一次抓取该视频号的登录卡片」,只做追溯与显示。

    def upsert_posts(self, finder_id, rows, account_id=""):
        """批量入库作品(抓取器用)。finder_id 为归属视频号;account_id 仅记抓取来源。"""
        finder_id = (finder_id or "").strip()
        if not finder_id:
            raise ValueError("upsert_posts 缺少 finder_id(视频号 ID)")
        c = self._conn()
        try:
            now = datetime.now().isoformat()
            for r in rows:
                sig = f"{r['read_count']}:{r['like_count']}:{r['comment_count']}"
                c.execute("""INSERT INTO posts(finder_id,object_id,account_id,export_id,title,cover_url,
                             cover_hash,cover_path,create_time,read_count,like_count,comment_count,
                             forward_count,fav_count,follow_count,visible_type,sticky_op,stat_sig,raw,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(finder_id,object_id) DO UPDATE SET
                      account_id=excluded.account_id,
                      export_id=excluded.export_id, title=excluded.title,
                      cover_url=excluded.cover_url, cover_hash=excluded.cover_hash, cover_path=excluded.cover_path,
                      create_time=excluded.create_time, read_count=excluded.read_count, like_count=excluded.like_count,
                      comment_count=excluded.comment_count, forward_count=excluded.forward_count,
                      fav_count=excluded.fav_count, follow_count=excluded.follow_count,
                      visible_type=excluded.visible_type, sticky_op=excluded.sticky_op,
                      stat_sig=excluded.stat_sig, raw=excluded.raw, updated_at=excluded.updated_at""",
                    (finder_id, r["object_id"], account_id or "", r["export_id"], r["title"],
                     r["cover_url"], r["cover_hash"], r.get("cover_path") or "",
                     r["create_time"], r["read_count"], r["like_count"], r["comment_count"],
                     r["forward_count"], r["fav_count"], r["follow_count"],
                     r["visible_type"], r["sticky_op"], sig, r["raw"], now))
            c.commit()
        finally:
            c.close()

    def posts_all_known(self, finder_id, object_ids):
        """早停判定:这一页的作品是否已全部在本地库(按视频号归属查)。

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
                f"SELECT COUNT(*) FROM posts WHERE finder_id=? AND object_id IN ({ph})",
                [finder_id] + ids).fetchone()[0]
            return int(n) >= len(ids)
        finally:
            c.close()

    def get_post(self, finder_id, object_id):
        c = self._conn()
        try:
            r = c.execute("SELECT object_id, export_id, visible_type, sticky_op, title, account_id "
                          "FROM posts WHERE finder_id=? AND object_id=?", (finder_id, object_id)).fetchone()
            if not r:
                return None
            return dict(object_id=r[0], export_id=r[1], visible_type=r[2], sticky_op=r[3],
                        title=r[4], account_id=r[5] or "")
        finally:
            c.close()

    def update_post_flags(self, finder_id, object_id, visible_type=None, sticky_op=None):
        """本地状态直接更新(写操作成功后调用,不回抓)。"""
        sets, args = [], []
        if visible_type is not None:
            sets.append("visible_type=?"); args.append(int(visible_type))
        if sticky_op is not None:
            sets.append("sticky_op=?"); args.append(int(sticky_op))
        if not sets:
            return
        sets.append("updated_at=?"); args.append(datetime.now().isoformat())
        args.extend([finder_id, object_id])
        c = self._conn()
        try:
            c.execute(f"UPDATE posts SET {', '.join(sets)} WHERE finder_id=? AND object_id=?", args)
            c.commit()
        finally:
            c.close()

    _SORTS = {"create_time": "create_time", "read_count": "read_count", "like_count": "like_count",
              "comment_count": "comment_count", "forward_count": "forward_count", "fav_count": "fav_count"}

    def query_posts(self, finder_id=None, q=None, visible=None, date_from=None, date_to=None,
                    sort="create_time", order="desc", limit=50, offset=0):
        """分页查询作品(按视频号归属)。visible: public/follow/hidden/sticky;日期按发布时间。

        sticky 作品(置顶)永远排在最前,组内再按所选排序。
        """
        col = self._SORTS.get(sort, "create_time")
        direc = "ASC" if str(order).lower() == "asc" else "DESC"
        where, args = [], []
        if finder_id:
            where.append("finder_id=?"); args.append(finder_id)
        if q:
            where.append("title LIKE ?"); args.append(f"%{q}%")
        if visible == "public":
            where.append("visible_type=1")
        elif visible == "hidden":
            where.append("visible_type=3")
        elif visible == "follow":
            where.append("visible_type=2")
        try:
            if date_from:
                d = datetime.strptime(date_from, "%Y-%m-%d")
                where.append("create_time>=?"); args.append(int(d.timestamp()))
            if date_to:
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
                f"""SELECT finder_id,object_id,account_id,export_id,title,cover_url,cover_path,
                            create_time,read_count,like_count,comment_count,forward_count,fav_count,
                            visible_type,sticky_op,updated_at
                     FROM posts{clause} ORDER BY {order_expr} LIMIT ? OFFSET ?""",
                args + [limit, offset]).fetchall()
            items = [dict(finder_id=r[0], object_id=r[1], account_id=r[2] or "", export_id=r[3],
                          title=r[4], cover_url=r[5], cover_path=r[6], create_time=r[7],
                          read_count=r[8], like_count=r[9], comment_count=r[10], forward_count=r[11],
                          fav_count=r[12], visible_type=r[13], sticky_op=r[14],
                          is_hidden=(r[13] == 3), is_sticky=(r[14] == 2),
                          updated_at=r[15]) for r in rows]
            return items, total
        finally:
            c.close()

    def posts_finders(self):
        """已入库作品的视频号清单(作品归属视图):每个视频号多少作品、最后更新、绑定卡片。"""
        c = self._conn()
        try:
            rows = c.execute("""SELECT finder_id, COUNT(*) AS works,
                        MAX(account_id) AS account_id, MAX(create_time) AS last_create,
                        MAX(updated_at) AS last_update,
                        SUM(CASE WHEN sticky_op=2 THEN 1 ELSE 0 END) AS sticky,
                        SUM(CASE WHEN visible_type=3 THEN 1 ELSE 0 END) AS hidden
                    FROM posts GROUP BY finder_id ORDER BY works DESC""").fetchall()
            return [dict(finder_id=r[0], works=r[1] or 0, account_id=r[2] or "",
                         last_create=r[3], last_update=r[4],
                         sticky=r[5] or 0, hidden=r[6] or 0) for r in rows]
        finally:
            c.close()

    # ---------- 抓取账本(按视频号记账,换卡片不用重跑全量) ----------
    def get_post_fetch_meta(self, finder_id):
        c = self._conn()
        try:
            r = c.execute("SELECT last_refresh,last_pages,today_date,today_requests,full_synced,account_id "
                          "FROM post_fetch_meta WHERE finder_id=?", (finder_id,)).fetchone()
            today = datetime.now().strftime("%Y-%m-%d")
            if not r:
                return dict(last_refresh=None, last_pages=0, today_requests=0,
                            full_synced=0, account_id="")
            return dict(last_refresh=r[0], last_pages=r[1] or 0,
                        today_requests=(r[3] or 0) if r[2] == today else 0,
                        full_synced=int(r[4] or 0), account_id=r[5] or "")
        finally:
            c.close()

    def set_post_fetch_meta(self, finder_id, account_id=None, last_refresh=None, last_pages=None,
                            add_requests=0, full_synced=None):
        today = datetime.now().strftime("%Y-%m-%d")
        c = self._conn()
        try:
            c.execute("INSERT OR IGNORE INTO post_fetch_meta(finder_id,account_id,last_refresh,last_pages,today_date,today_requests) "
                      "VALUES(?,?,?,?,?,0)", (finder_id, account_id or "", None, 0, today))
            if account_id:
                c.execute("UPDATE post_fetch_meta SET account_id=? WHERE finder_id=?", (account_id, finder_id))
            if add_requests > 0:
                c.execute("UPDATE post_fetch_meta SET today_requests=CASE WHEN today_date=? THEN today_requests+? ELSE ? END "
                          "WHERE finder_id=?", (today, add_requests, add_requests, finder_id))
            if last_refresh is not None:
                c.execute("UPDATE post_fetch_meta SET last_refresh=?, last_pages=? WHERE finder_id=?",
                          (last_refresh, last_pages or 0, finder_id))
            if full_synced is not None:
                c.execute("UPDATE post_fetch_meta SET full_synced=? WHERE finder_id=?",
                          (1 if full_synced else 0, finder_id))
            c.commit()
        finally:
            c.close()

    # ---------- 一次性迁移:作品归属 account_id -> finder_id ----------
    def migrate_posts_to_finder(self, fid_map=None, covers_root=None):
        """把作品归属主键从「登录卡片 account_id」迁到「视频号 finder_id」。幂等(已迁移直接返回)。

        fid_map: {account_id: finder_id}。取不到的用 "acct:<account_id>" 兜底 ——
                 宁可标成「未知视频号」,也不能把历史作品丢掉。
        covers_root: 封面根目录,默认 db 同级的 covers/。迁移后把
                 covers/<account_id>/ 搬到 covers/<finder_key>/,并按新 key 重算 cover_path。
        返回 dict(migrated, posts, meta, covers, unmapped)。
        """
        fid_map = {k: (v or "").strip() for k, v in (fid_map or {}).items() if k}
        res = {"migrated": False, "posts": 0, "meta": 0, "covers": 0, "unmapped": []}

        def fid_of(acc):
            fid = fid_map.get(acc or "")
            if fid:
                return fid
            if acc not in res["unmapped"]:
                res["unmapped"].append(acc)
            return "acct:" + (acc or "unknown")

        c = self._conn()
        try:
            pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
            if not pcols or "finder_id" in pcols:
                return res          # 表未建 / 已迁移
            c.execute("DROP INDEX IF EXISTS idx_posts_time")
            c.execute("DROP INDEX IF EXISTS idx_posts_acc")
            c.execute("ALTER TABLE posts RENAME TO posts_old")
            c.execute(POSTS_DDL)
            rows = c.execute(
                "SELECT account_id,object_id,export_id,title,cover_url,cover_hash,cover_path,"
                "create_time,read_count,like_count,comment_count,forward_count,fav_count,"
                "follow_count,visible_type,sticky_op,stat_sig,raw,updated_at "
                "FROM posts_old ORDER BY updated_at").fetchall()   # 同作品多卡片时,新的覆盖旧的
            data, acc2fid = [], {}
            for r in rows:
                acc = r[0] or ""
                fid = fid_of(acc)
                acc2fid[acc] = fid
                cp = r[6] or ""
                if cp and r[5]:
                    cp = "/media/covers/%s/%s.jpg" % (cover_key(fid), r[5])
                data.append((fid, r[1], acc, r[2], r[3], r[4], r[5], cp, r[7], r[8], r[9],
                             r[10], r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18]))
            c.executemany(
                "INSERT OR REPLACE INTO posts(finder_id,object_id,account_id,export_id,title,cover_url,"
                "cover_hash,cover_path,create_time,read_count,like_count,comment_count,forward_count,"
                "fav_count,follow_count,visible_type,sticky_op,stat_sig,raw,updated_at) "
                "VALUES(" + ",".join(["?"] * 20) + ")", data)
            c.execute("DROP TABLE posts_old")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_finder ON posts(finder_id)")
            res["posts"] = len(data)

            mcols = [r[1] for r in c.execute("PRAGMA table_info(post_fetch_meta)").fetchall()]
            if mcols and "finder_id" not in mcols:
                c.execute("ALTER TABLE post_fetch_meta RENAME TO meta_old")
                c.execute(META_DDL)
                mrows = c.execute("SELECT account_id,last_refresh,last_pages,today_date,"
                                  "today_requests,full_synced FROM meta_old").fetchall()
                c.executemany(
                    "INSERT OR REPLACE INTO post_fetch_meta(finder_id,account_id,last_refresh,last_pages,"
                    "today_date,today_requests,full_synced) VALUES(?,?,?,?,?,?,?)",
                    [(fid_of(r[0] or ""), r[0], r[1], r[2], r[3], r[4], r[5]) for r in mrows])
                c.execute("DROP TABLE meta_old")
                res["meta"] = len(mrows)
            c.commit()
            res["migrated"] = True
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

        # 封面目录搬家独立于事务:失败只影响封面显示(会重新下载),不影响作品归属
        if covers_root is None:
            covers_root = os.path.join(os.path.dirname(self.db_path or ""), "covers")
        res["covers"] = _move_cover_dirs(covers_root, acc2fid)
        logger.info(f"[作品归属迁移] 卡片->视频号: 作品 {res['posts']} 条, 账本 {res['meta']} 条, "
                    f"封面目录 {res['covers']} 个, 未识别视频号 {res['unmapped']}")
        return res

    def migrate_comments_to_finder(self, fid_map=None):
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
            # 作品反查兜底:卡片已被删除(配置里没有映射)时,用评论的 export_id
            # 去作品表反查归属视频号 —— 一条视频只属于一个视频号,反查是确定性的。
            res["tables"]["from_posts"] = self._resolve_by_posts(c)
            res["tables"]["video_stats"] = self._dedup_by_finder(c, "video_stats", fid_map, res)
            res["tables"]["auto_commented"] = self._dedup_by_finder(c, "auto_commented", fid_map, res)
            c.commit()
            return res
        finally:
            c.close()

    @staticmethod
    def _resolve_by_posts(c):
        """用作品表反查视频号,把「配置里已不存在的旧卡片」名下的评论归位。返回改动行数。

        判据:comments.export_id == posts.export_id -> posts.finder_id。
        只在作品表已有该作品、且其归属不是 acct: 兜底值时才采纳(否则无从判断)。
        表不存在(极老库)直接跳过,绝不让迁移抛错。
        """
        try:
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        except Exception:
            return 0
        if "posts" not in tables:
            return 0
        pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
        if "finder_id" not in pcols:
            return 0
        n = 0
        for t in ("comments", "delete_logs", "video_stats", "auto_commented", "own_comment_pending"):
            if t not in tables:
                continue
            try:
                rows = c.execute(f"""SELECT DISTINCT t.export_id, p.finder_id
                        FROM {t} t JOIN (
                            SELECT export_id, MIN(finder_id) AS finder_id FROM posts
                            WHERE COALESCE(export_id,'')<>'' AND finder_id NOT LIKE 'acct:%'
                            GROUP BY export_id) p ON p.export_id = t.export_id
                        WHERE COALESCE(t.finder_id,'')='' OR t.finder_id LIKE 'acct:%'""").fetchall()
                for eid, fid in rows:
                    if not fid or not eid or fid.startswith("acct:"):
                        continue
                    n += c.execute(
                        f"UPDATE {t} SET finder_id=? WHERE export_id=? "
                        "AND (COALESCE(finder_id,'')='' OR finder_id LIKE 'acct:%')",
                        (fid, eid)).rowcount or 0
            except Exception:
                continue
        return n

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
    def _own_exclusion(own_map=None):
        """生成「排除自己发出的评论」的 WHERE 片段,返回 (sql, args)。

        判据一:comment_id 在本工具登记表里(自动评论+置顶 / 自动回复 / 手动回复)。
        判据二:作者身份与本视频号自己一致 —— 优先视频号 id(权威,作者改名也不受影响),
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
                # 注意参数顺序必须与 SQL 里占位符出现的顺序一致:finder_id 在最前
                parts.append("(finder_id=? AND (%s))" % " OR ".join(cond))
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
                    f"""SELECT finder_id, {col}, COUNT(*) FROM comments
                        WHERE deleted=0 AND COALESCE({col},'')<>''
                        GROUP BY finder_id, {col}""").fetchall()
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
    def mark_own_comment(self, finder_id, comment_id, via, content=None, export_id=None,
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
                          (finder_id, content))
            c.commit()
        finally:
            c.close()

    def mark_own_comment_pending(self, finder_id, export_id, content, via, account_id=None):
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
                          (finder_id, account_id, export_id or "", content, via, datetime.now().isoformat()))
                c.commit()
        finally:
            c.close()

    def pending_own_comment_count(self, finder_id=None):
        c = self._conn()
        try:
            if finder_id:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending WHERE finder_id=?",
                              (finder_id,)).fetchone()
            else:
                r = c.execute("SELECT COUNT(*) FROM own_comment_pending").fetchone()
            return r[0] if r else 0
        finally:
            c.close()

    def reconcile_own_comments(self, finder_id=None, max_age_days=30):
        """把「当时没拿到 comment_id」的自己评论补登进 own_comments。返回补登条数。

        匹配依据:同视频号 + export_id(若记录里没有则不限)+ 内容完全相同 +
                  尚未在 own_comments 里的评论。内容逐字相同且是同一作品,
        基本只可能是自己刚发的那条 —— 客户不会复述我方话术。
        补不上的保留待确认;超过 max_age_days 仍未匹配的丢弃,避免无限堆积。
        """
        c = self._conn()
        try:
            rows = c.execute(
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
                        WHERE finder_id=? AND content=?
                          AND (?='' OR export_id=?)
                          AND deleted=0 AND COALESCE(comment_id,'')<>''
                          AND comment_id NOT IN
                              (SELECT comment_id FROM own_comments WHERE COALESCE(comment_id,'')<>'')
                        ORDER BY create_time ASC, id ASC LIMIT 1""",
                    (fid, content, exp or "", exp or "")).fetchone()
                if not row:
                    continue
                c.execute("INSERT OR IGNORE INTO own_comments(finder_id,account_id,comment_id,via,created_at) "
                          "VALUES(?,?,?,?,?)",
                          (fid, acc, row[0], via, datetime.now().isoformat()))
                c.execute("DELETE FROM own_comment_pending WHERE id=?", (pid,))
                matched += 1
                logger.info("[%s] 补登自己发出的评论 %s(via=%s): %s",
                            fid, row[0], via, (content or "")[:20])
            # 超龄未匹配:丢弃(评论可能早被删/抓不到),不让待确认表无限增长
            c.execute("DELETE FROM own_comment_pending WHERE created_at < ?",
                      ((datetime.now() - timedelta(days=max_age_days)).isoformat(),))
            c.commit()
            return matched
        finally:
            c.close()

    # ==================== 自动发作品(发布队列 + 标题池) ====================

    # ---------- 标题池 ----------
    def add_titles(self, contents, finder_id=""):
        """批量加标题(一行一条)。返回实际新增条数;空行/同池内内容重复的跳过。"""
        now = datetime.now().isoformat()
        c = self._conn()
        try:
            exist = {r[0] for r in c.execute(
                "SELECT content FROM publish_titles WHERE finder_id=?", (finder_id or "",))}
            n = 0
            for raw in contents:
                t = (raw or "").strip()
                if not t or t in exist:
                    continue
                c.execute("""INSERT INTO publish_titles(finder_id,content,enabled,used_count,
                             used_task_id,last_used_at,created_at) VALUES(?,?,1,0,0,NULL,?)""",
                          (finder_id or "", t, now))
                exist.add(t)
                n += 1
            c.commit()
            return n
        finally:
            c.close()

    def query_titles(self, finder_id=None, include_used=True, limit=500):
        """查标题池。finder_id=None 查全部;''=只看通用池。未用的排前面。"""
        c = self._conn()
        try:
            where, args = [], []
            if finder_id is not None:
                where.append("finder_id=?"); args.append(finder_id)
            if not include_used:
                where.append("used_count=0")
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            rows = c.execute(
                f"""SELECT id,finder_id,content,enabled,used_count,used_task_id,last_used_at,created_at
                    FROM publish_titles{clause} ORDER BY used_count ASC, id ASC LIMIT ?""",
                args + [limit]).fetchall()
            cols = ("id", "finder_id", "content", "enabled", "used_count",
                    "used_task_id", "last_used_at", "created_at")
            return [dict(zip(cols, r)) for r in rows]
        finally:
            c.close()

    def title_pool_stats(self):
        """各池标题余量:{"<finder_id或''>": {"total":n,"unused":m}}。"""
        c = self._conn()
        try:
            out = {}
            for fid, total, unused in c.execute(
                    """SELECT finder_id, COUNT(*), SUM(CASE WHEN used_count=0 THEN 1 ELSE 0 END)
                       FROM publish_titles WHERE enabled=1 GROUP BY finder_id"""):
                out[fid or ""] = {"total": total or 0, "unused": unused or 0}
            return out
        finally:
            c.close()

    def delete_title(self, title_id):
        c = self._conn()
        try:
            c.execute("DELETE FROM publish_titles WHERE id=?", (title_id,))
            c.commit()
        finally:
            c.close()

    def clear_titles(self, finder_id=None, only_used=False):
        """清标题池。only_used=True 只清已用过的(方便补新标题)。"""
        c = self._conn()
        try:
            where, args = [], []
            if finder_id is not None:
                where.append("finder_id=?"); args.append(finder_id)
            if only_used:
                where.append("used_count>0")
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            n = c.execute(f"SELECT COUNT(*) FROM publish_titles{clause}", args).fetchone()[0]
            c.execute(f"DELETE FROM publish_titles{clause}", args)
            c.commit()
            return n
        finally:
            c.close()

    def take_title(self, finder_id):
        """取一条可用标题并立即标记已用(一条一用,用完即弃)。

        优先该视频号专属池,取完自动回落到通用池(finder_id='')。
        返回 (title_id, content);两个池都没有可用标题时返回 (0, '')。
        """
        now = datetime.now().isoformat()
        c = self._conn()
        try:
            for pool in (finder_id or "", ""):
                if not pool and finder_id:
                    pass  # 仍要试通用池
                row = c.execute(
                    """SELECT id,content FROM publish_titles
                       WHERE finder_id=? AND enabled=1 AND used_count=0
                       ORDER BY id ASC LIMIT 1""", (pool,)).fetchone()
                if row:
                    c.execute("UPDATE publish_titles SET used_count=1, last_used_at=? WHERE id=?",
                              (now, row[0]))
                    c.commit()
                    return int(row[0]), row[1]
            return 0, ""
        finally:
            c.close()

    def release_title(self, title_id):
        """发布失败时把标题归还池子(没真正发出去,标题还能给下一条用)。"""
        if not title_id:
            return
        c = self._conn()
        try:
            c.execute("UPDATE publish_titles SET used_count=0, used_task_id=0 WHERE id=?",
                      (int(title_id),))
            c.commit()
        finally:
            c.close()

    def bind_title_task(self, title_id, task_id):
        c = self._conn()
        try:
            c.execute("UPDATE publish_titles SET used_task_id=? WHERE id=?",
                      (int(task_id), int(title_id)))
            c.commit()
        finally:
            c.close()

    # ---------- 发布队列 ----------
    def enqueue_task(self, finder_id, src_path, file_name, file_size, file_fp, title="",
                     title_id=0, account_id=""):
        """入队(幂等)。返回 (task_id, created)。同 (finder_id,file_fp) 已存在则不新建。"""
        c = self._conn()
        try:
            row = c.execute(
                "SELECT id FROM publish_tasks WHERE finder_id=? AND file_fp=?",
                (finder_id, file_fp)).fetchone()
            if row:
                return int(row[0]), False
            cur = c.execute(
                """INSERT INTO publish_tasks(finder_id,account_id,src_path,file_name,file_size,
                     file_fp,title,title_id,status,step,attempts,last_error,queued_at)
                   VALUES(?,?,?,?,?,?,?,?,'pending','',0,'',?)""",
                (finder_id, account_id, src_path, file_name, file_size, file_fp,
                 title, int(title_id or 0), datetime.now().isoformat()))
            c.commit()
            return int(cur.lastrowid), True
        finally:
            c.close()

    def get_task(self, task_id):
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
            if not r:
                return None
            return dict(zip([d[0] for d in c.execute(
                "SELECT * FROM publish_tasks LIMIT 0").description], r))
        finally:
            c.close()

    def query_tasks(self, status=None, finder_id=None, limit=200, offset=0):
        """查发布队列,返回 (rows, total)。status 可传 'pending,running' 这种逗号串。"""
        c = self._conn()
        try:
            where, args = [], []
            if status:
                flags = [x.strip() for x in str(status).split(",") if x.strip()]
                if flags:
                    where.append("status IN (" + ",".join("?" * len(flags)) + ")")
                    args.extend(flags)
            if finder_id:
                where.append("finder_id=?"); args.append(finder_id)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            total = c.execute(f"SELECT COUNT(*) FROM publish_tasks{clause}", args).fetchone()[0]
            rows = c.execute(
                f"""SELECT * FROM publish_tasks{clause}
                    ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                             id ASC LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
            cols = [d[0] for d in c.execute("SELECT * FROM publish_tasks LIMIT 0").description]
            return [dict(zip(cols, r)) for r in rows], total
        finally:
            c.close()

    def set_task_status(self, task_id, status=None, step=None, error=None, object_id=None,
                        export_id=None, account_id=None, started=False, finished=False,
                        attempts=None, archived_name=None):
        sets, args = [], []
        for col, val in (("status", status), ("step", step), ("last_error", error),
                         ("object_id", object_id), ("export_id", export_id),
                         ("account_id", account_id), ("attempts", attempts),
                         ("archived_name", archived_name)):
            if val is not None:
                sets.append(f"{col}=?"); args.append(val)
        if started:
            sets.append("started_at=?"); args.append(datetime.now().isoformat())
        if finished:
            sets.append("finished_at=?"); args.append(datetime.now().isoformat())
        if not sets:
            return
        c = self._conn()
        try:
            c.execute(f"UPDATE publish_tasks SET {','.join(sets)} WHERE id=?",
                      args + [int(task_id)])
            c.commit()
        finally:
            c.close()

    def update_task_title(self, task_id, title):
        c = self._conn()
        try:
            c.execute("UPDATE publish_tasks SET title=? WHERE id=?", (title or "", int(task_id)))
            c.commit()
        finally:
            c.close()

    def reset_task(self, task_id, clear_error=True):
        """把任务重置回 pending(人工点「重试」用)。"""
        c = self._conn()
        try:
            c.execute("""UPDATE publish_tasks SET status='pending', step='', started_at=NULL,
                         finished_at=NULL""" + (", last_error=''" if clear_error else "") +
                      " WHERE id=?", (int(task_id),))
            c.commit()
        finally:
            c.close()

    def reset_running_tasks(self):
        """启动时收尾:上次进程死在 running 的任务不能自动重发(怕实际已发出去),
        统一转 need_review 等人工确认。返回受影响条数。"""
        c = self._conn()
        try:
            n = c.execute("SELECT COUNT(*) FROM publish_tasks WHERE status='running'").fetchone()[0]
            if n:
                c.execute("""UPDATE publish_tasks SET status='need_review',
                             last_error=COALESCE(NULLIF(last_error,''),
                               '进程中断,不确定是否已发出,请到视频号后台核对后再决定重试')""")
            c.commit()
            return n
        finally:
            c.close()

    def daily_success_count(self, finder_id, date_str):
        """某视频号某天成功发布条数(date_str = YYYY-MM-DD,按 finished_at 统计)。"""
        c = self._conn()
        try:
            return c.execute(
                """SELECT COUNT(*) FROM publish_tasks
                   WHERE finder_id=? AND status='success' AND finished_at LIKE ?""",
                (finder_id, (date_str or "") + "%")).fetchone()[0]
        finally:
            c.close()

    def last_success_at(self, finder_id):
        c = self._conn()
        try:
            r = c.execute(
                """SELECT MAX(finished_at) FROM publish_tasks
                   WHERE finder_id=? AND status='success'""", (finder_id,)).fetchone()
            return (r[0] if r else "") or ""
        finally:
            c.close()

    def delete_task(self, task_id):
        c = self._conn()
        try:
            c.execute("DELETE FROM publish_tasks WHERE id=?", (int(task_id),))
            c.commit()
        finally:
            c.close()
