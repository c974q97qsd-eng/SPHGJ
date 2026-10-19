# -*- coding: utf-8 -*-
"""rev24 补丁 2.5:卡片已删除时,用「评论 export_id -> 作品归属 -> 视频号」反查兜底。

干跑发现母本还有 610 条评论挂在已删除的旧卡片下(uspoloassn3/uspoloassn5),
配置里已经没有它们,但那条视频在作品表里查得到归属视频号 —— 一条视频只属于一个视频号,
所以按 export_id 反查是确定性的。
"""
import io
import sys

P = r"D:\SPHGJ\backend\storage.py"
s = io.open(P, encoding="utf-8").read()

OLD = '''            res["tables"]["video_stats"] = self._dedup_by_finder(c, "video_stats", fid_map, res)
            res["tables"]["auto_commented"] = self._dedup_by_finder(c, "auto_commented", fid_map, res)'''
NEW = '''            # 作品反查兜底:卡片已被删除(配置里没有映射)时,用评论的 export_id
            # 去作品表反查归属视频号 —— 一条视频只属于一个视频号,反查是确定性的。
            res["tables"]["from_posts"] = self._resolve_by_posts(c)
            res["tables"]["video_stats"] = self._dedup_by_finder(c, "video_stats", fid_map, res)
            res["tables"]["auto_commented"] = self._dedup_by_finder(c, "auto_commented", fid_map, res)'''

OLD2 = '''    @staticmethod
    def _dedup_by_finder(c, table, fid_map, res):'''
NEW2 = '''    @staticmethod
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
    def _dedup_by_finder(c, table, fid_map, res):'''

fail = []
for tag, old, new in (("call", OLD, NEW), ("method", OLD2, NEW2)):
    if s.count(old) != 1:
        fail.append(f"{tag} 命中 {s.count(old)} 次")
        continue
    s = s.replace(old, new, 1)
if fail:
    print("PATCH FAIL", fail)
    sys.exit(1)
io.open(P, "w", encoding="utf-8", newline="").write(s)
s2 = io.open(P, encoding="utf-8").read()
ok = "def _resolve_by_posts" in s2 and 'res["tables"]["from_posts"]' in s2
print("SELFCHECK", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 2)
