# -*- coding: utf-8 -*-
"""rev56 补丁:storage 各归属表补 platform 列（默认 'wx'）。

用法: python tools/patch_rev56.py
幂等: 再跑一次会打印「已打过」并跳过。
"""
import io
import sys

P = r"D:\SPHGJ\backend\storage.py"

ANCHOR_CONST = '''# 只需补 finder_id 列(不必重建表)的评论域表
COMMENT_FID_TABLES = ("comments", "own_comments", "own_comment_pending", "delete_logs")
'''

NEW_CONST = '''# 只需补 finder_id 列(不必重建表)的评论域表
COMMENT_FID_TABLES = ("comments", "own_comments", "own_comment_pending", "delete_logs")

# rev56: 需要 platform 列(默认 'wx' = 视频号)的归属表。
# 只加列、不改主键 —— 归属键仍是 finder_id，它的语义扩成「平台内的账号本体 id」
# (wx=视频号 finder_id / dy=抖音 sec_uid)。理由见 backend/platforms.py 顶部。
# 加列的收益: 展示分组、跨平台统计、以及给未来「同号多平台」留位置;
# 不改主键的收益: 250 处 SQL / 索引 / UNIQUE 约束全部不用动，回归风险接近零。
PLATFORM_TABLES = (
    "comments", "video_stats", "auto_commented", "own_comments", "own_comment_pending",
    "delete_logs", "posts", "post_fetch_meta", "publish_tasks", "publish_titles",
    "video_activity",
)
'''

ANCHOR_CALL = '''        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀
        c.commit()
        c.close()'''

NEW_CALL = '''        # rev56: 各归属表补 platform 列(幂等)。放在最后 —— posts 表可能被
        # migrate_posts_to_finder 重建，那里会再调一次同样的方法。
        self._migrate_platform_column(c)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀
        c.commit()
        c.close()'''

METHOD = '''
    # ==================== rev56: platform 列 ====================

    def _migrate_platform_column(self, c=None):
        """给各归属表补 platform 列(默认 'wx')。幂等，可在任意入口反复调用。

        为什么不做成主键的一部分：`finder_id` 在各平台内本来就唯一，把它升级成
        复合主键只会逼着所有 SQL / 索引 / UNIQUE 一起改，收益为零。platform 列
        只承担「标记归属域」这一件事（见 backend/platforms.py）。

        `ALTER TABLE ... ADD COLUMN ... DEFAULT 'wx'` 在 SQLite 里是只改元数据的
        操作（常量默认值），对几十万行的 comments 表也是瞬时的，不会锁库。
        """
        own = c is None
        if own:
            c = self._conn()
        try:
            existing = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            added = []
            for t in PLATFORM_TABLES:
                if t not in existing:
                    continue
                cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % t).fetchall()}
                if "platform" in cols:
                    continue
                c.execute("ALTER TABLE %s ADD COLUMN platform TEXT DEFAULT 'wx'" % t)
                added.append(t)
            if own:
                c.commit()
            if added:
                logger.info("数据库补 platform 列(默认 wx): " + ", ".join(added))
            return added
        finally:
            if own:
                c.close()

    def platform_of(self, finder_id):
        """反查某个归属键属于哪个平台（作品表/评论表里取）。

        查不到时按 'wx' 兜底 —— 历史数据全是视频号，这个兜底是保守的。
        """
        if not finder_id:
            return "wx"
        for sql in ("SELECT platform FROM comments WHERE finder_id=? AND platform IS NOT NULL LIMIT 1",
                    "SELECT platform FROM posts WHERE finder_id=? AND platform IS NOT NULL LIMIT 1"):
            try:
                c = self._conn()
                try:
                    row = c.execute(sql, (finder_id,)).fetchone()
                finally:
                    c.close()
                if row and row[0]:
                    return row[0]
            except Exception:
                continue
        return "wx"
'''

ANCHOR_IDEMPOTENT = '''            pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
            if not pcols or "finder_id" in pcols:
                return res          # 表未建 / 已迁移'''

NEW_IDEMPOTENT = '''            pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
            if not pcols or "finder_id" in pcols:
                return res          # 表未建 / 已迁移'''

PATCHES = [
    ("const", ANCHOR_CONST, NEW_CONST),
    ("call", ANCHOR_CALL, NEW_CALL),
    ("method", '    def _conn(self):\n        c = sqlite3.connect(self.db_path, timeout=30)',
     METHOD + '\n    def _conn(self):\n        c = sqlite3.connect(self.db_path, timeout=30)'),
]


def main():
    s = io.open(P, encoding="utf-8").read()
    for tag, old, new in PATCHES:
        if old not in s:
            print("  [跳过] %s 找不到锚点(可能已打过)" % tag)
            continue
        assert s.count(old) == 1, "%s 锚点命中 %d 次" % (tag, s.count(old))
        s = s.replace(old, new, 1)
        print("  [ok] %s" % tag)

    # migrate_posts_to_finder 重建 posts 表后会丢掉 platform 列，那里补一次
    anchor = '''            c.execute("DROP TABLE posts_old")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(create_time DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_posts_finder ON posts(finder_id)")'''
    new = anchor + '''
            # 重建过 posts 表 -> platform 列没了，补回来（否则抖音作品会被当成视频号）
            self._migrate_platform_column(c)'''
    if anchor in s and "重建过 posts 表 -> platform 列没了" not in s:
        assert s.count(anchor) == 1, s.count(anchor)
        s = s.replace(anchor, new, 1)
        print("  [ok] posts 重建后补 platform")

    io.open(P, "w", encoding="utf-8", newline="").write(s)

    # ---- 自校验 ----
    checks = [
        ("PLATFORM_TABLES", "PLATFORM_TABLES = (" in s),
        ("_migrate_platform_column", "def _migrate_platform_column" in s),
        ("platform_of", "def platform_of" in s),
        ("init 调用", "self._migrate_platform_column(c)" in s),
        ("posts 重建后补", "重建过 posts 表 -> platform 列没了" in s),
    ]
    bad = [k for k, v in checks if not v]
    print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
    try:
        compile(s, P, "exec")
        print("语法 OK")
    except SyntaxError as e:
        print("语法错误:", e)
        sys.exit(1)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
