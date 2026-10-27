# -*- coding: utf-8 -*-
"""rev52 step1: storage 新增 video_activity(每日播放数采样)表与三个方法。"""
import io
import sys

P = r"D:\SPHGJ\backend\storage.py"
s = io.open(P, encoding="utf-8").read()
orig = s

PATCHES = []

# 1) 建表:紧跟 daily_write_count 之后
PATCHES.append((
    '''        c.execute("""CREATE TABLE IF NOT EXISTS delete_logs(''',
    '''        # rev52: 播放数每日采样。分区判定「最近 3 日播放增长」的数据来源。
        # 同一天多次采样只留最后一次(day 为主键的一部分),避免一天堆几十行。
        c.execute("""CREATE TABLE IF NOT EXISTS video_activity(
            finder_id TEXT, export_id TEXT, day TEXT,
            read_count INTEGER, like_count INTEGER, comment_count INTEGER,
            account_id TEXT, updated_at TEXT,
            PRIMARY KEY(finder_id, export_id, day))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vactivity_finder_day "
                  "ON video_activity(finder_id, day)")
        c.execute("""CREATE TABLE IF NOT EXISTS delete_logs(''',
    "建 video_activity 表",
))

# 2) 方法:插在 set_video_comment_count 之后(reset_video_comment_count 之前)
ANCHOR = "    def reset_video_comment_count(self, finder_id=None, export_id=None):"
NEW_METHODS = '''    # ==================== rev52: 播放数每日采样(分区用) ====================

    def upsert_video_activity(self, finder_id, items, account_id=None):
        """批量写入当日播放数采样。items = [{"objectId","readCount","likeCount","commentCount"}]

        同一天重复写入会覆盖成当次读数(只留最后一条),所以日末那次读数即当日快照。
        字段缺失的项直接跳过 —— 宁可少一行采样,也不要把「没返回」写成 0 污染基线。
        """
        if not items:
            return 0
        day = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().isoformat()
        rows = []
        for v in items:
            oid = v.get("objectId")
            if not oid:
                continue
            rc = v.get("readCount")
            if rc is None:
                continue
            try:
                rc = int(rc)
            except (TypeError, ValueError):
                continue
            lc = v.get("likeCount")
            cc = v.get("commentCount")
            try:
                lc = int(lc) if lc is not None else None
            except (TypeError, ValueError):
                lc = None
            try:
                cc = int(cc) if cc is not None else None
            except (TypeError, ValueError):
                cc = None
            rows.append((finder_id, oid, day, rc, lc, cc, account_id or "", now))
        if not rows:
            return 0
        c = self._conn()
        try:
            c.executemany("""INSERT INTO video_activity(
                    finder_id,export_id,day,read_count,like_count,comment_count,
                    account_id,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(finder_id,export_id,day) DO UPDATE SET
                    read_count=excluded.read_count,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    account_id=COALESCE(NULLIF(excluded.account_id,''), account_id),
                    updated_at=excluded.updated_at""", rows)
            c.commit()
        finally:
            c.close()
        return len(rows)

    def get_activity_baseline(self, finder_id, days=3):
        """取每个作品「days 天前或更早的最近一天」的播放数,作为增长计算基线。

        返回 {export_id: read_count}。历史不足时该方法返回空 dict,调用方走降级链。
        用 MAX(day) 配 bare column 是 SQLite 的文档化行为(取 MAX 行的同列值),
        比自连接或窗口函数快得多,表再大也是一次索引扫描。
        """
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
        c = self._conn()
        try:
            rows = c.execute(
                """SELECT export_id, read_count, MAX(day) FROM video_activity
                   WHERE finder_id=? AND day<=? GROUP BY export_id""",
                (finder_id, cutoff)).fetchall()
        finally:
            c.close()
        out = {}
        for oid, rc, _d in rows or []:
            if oid and rc is not None:
                out[oid] = int(rc)
        return out

    def prune_video_activity(self, keep_days=30):
        """清掉过老的采样(默认留 30 天)。分区只需要 3 日前的基线,留 30 天足够回溯。"""
        cutoff = (datetime.now() - timedelta(days=max(7, int(keep_days)))).strftime("%Y-%m-%d")
        c = self._conn()
        try:
            cur = c.execute("DELETE FROM video_activity WHERE day<?", (cutoff,))
            c.commit()
            return cur.rowcount or 0
        finally:
            c.close()

'''
PATCHES.append((ANCHOR, NEW_METHODS + ANCHOR, "插入 activity 三个方法"))

for old, new, tag in PATCHES:
    if s.count(old) != 1:
        print("FAIL 命中 %d 次: %s" % (s.count(old), tag))
        sys.exit(1)
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# 自校验
chk = [
    ("video_activity(" in s, "建表 DDL"),
    ("def upsert_video_activity" in s, "upsert 方法"),
    ("def get_activity_baseline" in s, "baseline 方法"),
    ("def prune_video_activity" in s, "prune 方法"),
    ("MAX(day) FROM video_activity" in s, "baseline SQL"),
    (s.count("def reset_video_comment_count") == 1, "锚点唯一"),
    ("timedelta" in s, "timedelta 已导入?"),
]
for ok, tag in chk:
    print(("PASS " if ok else "FAIL ") + tag)
print("bytes %d -> %d" % (len(orig.encode()), len(s.encode())))
