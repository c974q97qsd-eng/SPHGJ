# -*- coding: utf-8 -*-
"""rev56b 补丁:storage 的写入路径支持 platform（默认 'wx'，老调用零改动）。

改 4 处：
  1. upsert_comment(..., platform="wx")   —— 评论入库带平台标记
  2. set_video_comment_count(..., platform="wx")
  3. upsert_video_activity(..., platform="wx")
  4. comment_finders() 返回 platform（前端按平台分组用）

为什么全是**可选关键字参数**：这三处被视频号链路调用了几百次，加必填参数会
逼着所有调用点一起改；默认 'wx' 让老代码行为一字不变。

用法: python tools/patch_rev56b.py
"""
import io
import sys

P = r"D:\SPHGJ\backend\storage.py"

PATCHES = [
    # ---------- 1. upsert_comment ----------
    ("upsert 签名",
     '    def upsert_comment(self, finder_id, export_id, comment, account_id=None):',
     '    def upsert_comment(self, finder_id, export_id, comment, account_id=None,\n'
     '                       platform="wx"):'),

    ("upsert INSERT",
     '''        c.execute("""INSERT OR IGNORE INTO comments
            (finder_id,account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (finder_id, account_id, export_id, cid, author_id, comment.get("commentNickname"),
             comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0),
             int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, 0,
             json.dumps(comment, ensure_ascii=False), datetime.now().isoformat()))''',
     '''        c.execute("""INSERT OR IGNORE INTO comments
            (finder_id,account_id,export_id,comment_id,author_id,nickname,content,head_url,create_time,like_count,read_flag,replied,raw,fetched_at,platform)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (finder_id, account_id, export_id, cid, author_id, comment.get("commentNickname"),
             comment.get("commentContent"), comment.get("commentHeadurl"),
             int(comment.get("commentCreatetime", 0) or 0),
             int(comment.get("commentLikeCount", 0) or 0),
             1 if comment.get("readFlag") else 0, 0,
             json.dumps(comment, ensure_ascii=False), datetime.now().isoformat(),
             platform or "wx"))'''),

    # ---------- 2. set_video_comment_count ----------
    ("vcc 签名",
     '    def set_video_comment_count(self, finder_id, export_id, count, account_id=None):',
     '    def set_video_comment_count(self, finder_id, export_id, count, account_id=None,\n'
     '                                platform="wx"):'),

    ("vcc INSERT",
     '''        c.execute("""INSERT INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id)
            VALUES(?,?,?,?,?)
            ON CONFLICT(finder_id,export_id) DO UPDATE SET
                comment_count=excluded.comment_count,
                updated_at=excluded.updated_at,
                account_id=COALESCE(NULLIF(excluded.account_id,''), account_id)""",
                  (finder_id, export_id, new, datetime.now().isoformat(), account_id or ""))''',
     '''        c.execute("""INSERT INTO video_stats(finder_id,export_id,comment_count,updated_at,account_id,platform)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(finder_id,export_id) DO UPDATE SET
                comment_count=excluded.comment_count,
                updated_at=excluded.updated_at,
                account_id=COALESCE(NULLIF(excluded.account_id,''), account_id),
                platform=COALESCE(NULLIF(excluded.platform,''), platform)""",
                  (finder_id, export_id, new, datetime.now().isoformat(), account_id or "",
                   platform or "wx"))'''),

    # ---------- 3. upsert_video_activity ----------
    ("activity 签名",
     '    def upsert_video_activity(self, finder_id, items, account_id=None):',
     '    def upsert_video_activity(self, finder_id, items, account_id=None, platform="wx"):'),

    ("activity row",
     '            rows.append((finder_id, oid, day, rc, lc, cc, account_id or "", now))',
     '            rows.append((finder_id, oid, day, rc, lc, cc, account_id or "", now,\n'
     '                         platform or "wx"))'),

    ("activity INSERT",
     '''            c.executemany("""INSERT INTO video_activity(
                    finder_id,export_id,day,read_count,like_count,comment_count,
                    account_id,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(finder_id,export_id,day) DO UPDATE SET
                    read_count=excluded.read_count,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    account_id=COALESCE(NULLIF(excluded.account_id,''), account_id),
                    updated_at=excluded.updated_at""", rows)''',
     '''            c.executemany("""INSERT INTO video_activity(
                    finder_id,export_id,day,read_count,like_count,comment_count,
                    account_id,updated_at,platform)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(finder_id,export_id,day) DO UPDATE SET
                    read_count=excluded.read_count,
                    like_count=excluded.like_count,
                    comment_count=excluded.comment_count,
                    account_id=COALESCE(NULLIF(excluded.account_id,''), account_id),
                    updated_at=excluded.updated_at,
                    platform=COALESCE(NULLIF(excluded.platform,''), platform)""", rows)'''),

    # ---------- 4. comment_finders 带 platform ----------
    ("finders SQL",
     '''            rows = c.execute("""SELECT finder_id, COUNT(*) AS comments,
                        MAX(account_id) AS account_id, MAX(fetched_at) AS last_fetched
                    FROM comments WHERE deleted=0 GROUP BY finder_id
                    ORDER BY comments DESC""").fetchall()
            return [dict(finder_id=r[0] or "", comments=r[1] or 0,
                         account_id=r[2] or "", last_fetched=r[3]) for r in rows]''',
     '''            rows = c.execute("""SELECT finder_id, COUNT(*) AS comments,
                        MAX(account_id) AS account_id, MAX(fetched_at) AS last_fetched,
                        MAX(platform) AS platform
                    FROM comments WHERE deleted=0 GROUP BY finder_id
                    ORDER BY comments DESC""").fetchall()
            return [dict(finder_id=r[0] or "", comments=r[1] or 0,
                         account_id=r[2] or "", last_fetched=r[3],
                         platform=(r[4] or "wx")) for r in rows]'''),
]


def main():
    s = io.open(P, encoding="utf-8").read()
    for tag, old, new in PATCHES:
        if old not in s:
            print("  [跳过] %s 找不到锚点(可能已打过)" % tag)
            continue
        if s.count(old) != 1:
            print("  [失败] %s 锚点命中 %d 次" % (tag, s.count(old)))
            return 1
        s = s.replace(old, new, 1)
        print("  [ok] %s" % tag)
    io.open(P, "w", encoding="utf-8", newline="").write(s)

    checks = [
        ('platform="wx"):' in s, "upsert_comment 有 platform 参数"),
        ("raw,fetched_at,platform)" in s, "comments INSERT 带 platform"),
        ("account_id,platform)" in s.replace(" ", ""), "video_stats INSERT 带 platform"),
        ("updated_at,platform)" in s, "video_activity INSERT 带 platform"),
        ("MAX(platform) AS platform" in s, "comment_finders 返回 platform"),
        ("excluded.platform" in s, "冲突时不把 platform 打回空"),
    ]
    bad = [k for ok, k in checks if not ok]
    print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
    try:
        compile(s, P, "exec")
        print("语法 OK")
    except SyntaxError as e:
        print("语法错误:", e)
        return 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
