# -*- coding: utf-8 -*-
"""rev23-storage: posts / post_fetch_meta 归属主键 account_id -> finder_id(视频号 ID)。"""
import io, sys

P = r"D:\SPHGJ\backend\storage.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# ---------- 1. import re ----------
old = "import logging\nimport os\nimport json\nimport sqlite3\n"
new = "import logging\nimport os\nimport json\nimport re\nimport sqlite3\n"
assert s.count(old) == 1, "import block"
s = s.replace(old, new, 1)

# ---------- 2. 模块级常量 + cover_key + 封面目录搬家 ----------
old = '''logger = logging.getLogger("sphgj")


class Storage:'''
new = '''logger = logging.getLogger("sphgj")

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


def cover_key(finder_id):
    """封面子目录名:按视频号 finder_id 取目录(仅替换 Windows 非法字符)。

    未识别的历史账号用 "acct:<id>" 兜底,目录名转成 "acct_<id>"(冒号不能做目录名)。
    """
    fid = (finder_id or "").strip()
    if not fid:
        return "unknown"
    if fid.startswith("acct:"):
        fid = "acct_" + fid[5:]
    return re.sub(r"[^0-9A-Za-z_@.\\-]", "_", fid)


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


class Storage:'''
assert s.count(old) == 1, "module header"
s = s.replace(old, new, 1)

# ---------- 3. _init 建表 ----------
old = '''        # 作品管理:作品本地库 + 抓取账本 + 本工具发出的评论(自动回复/自动评论/手动回复)
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
            full_synced INTEGER DEFAULT 0)""")'''
new = '''        # 作品管理:作品本地库 + 抓取账本 + 本工具发出的评论(自动回复/自动评论/手动回复)
        # 归属主键 = finder_id(视频号 ID);account_id 只记「最后抓取它的登录卡片」,不参与主键。
        c.execute(POSTS_DDL)
        c.execute(META_DDL)'''
assert s.count(old) == 1, "_init ddl"
s = s.replace(old, new, 1)

old = '''        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_acc ON posts(account_id)")'''
new = '''        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_finder ON posts(finder_id)")'''
assert s.count(old) == 1, "_init index"
s = s.replace(old, new, 1)

# ---------- 4. 整块替换「作品管理」 ----------
START = "    # ==================== 作品管理 ===================="
END = "    @staticmethod\n    def _own_exclusion(own_map=None):"
i0 = s.index(START)
i1 = s.index(END)
NEW_BLOCK = '''    # ==================== 作品管理 ====================
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

'''
s = s[:i0] + NEW_BLOCK + s[i1:]

assert s != orig
io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------- 自校验 ----------
chk = [
    ("POSTS_DDL 存在", "POSTS_DDL = " in s),
    ("META_DDL 存在", "META_DDL = " in s),
    ("cover_key 存在", "def cover_key(" in s),
    ("迁移函数存在", "def migrate_posts_to_finder(" in s),
    ("upsert 按 finder", "def upsert_posts(self, finder_id, rows, account_id=\"\")" in s),
    ("query 按 finder", "def query_posts(self, finder_id=None" in s),
    ("账本按 finder", "def get_post_fetch_meta(self, finder_id)" in s),
    ("旧 account 主键已去", "PRIMARY KEY(account_id, object_id)" not in s),
    ("旧 idx_posts_acc 已去", "idx_posts_acc" not in s),
]
bad = [n for n, ok in chk if not ok]
print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
sys.exit(0 if not bad else 1)
