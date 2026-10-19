# -*- coding: utf-8 -*-
"""rev24 干跑:在母本库的【副本】上验证评论归属迁移(不碰生产库)。

用法:python tools/dryrun_rev24.py <db_copy> <config_copy>
"""
import json
import sys

sys.path.insert(0, r"D:\SPHGJ")
from backend.storage import Storage  # noqa: E402

db, cfg = sys.argv[1], sys.argv[2]
config = json.load(open(cfg, encoding="utf-8"))
fid_map = {}
for a in config.get("accounts", []):
    fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()
    if fid and a.get("id"):
        fid_map[a["id"]] = fid
print("配置映射:", {k: v[:12] + "…" for k, v in fid_map.items()})

st = Storage(db)


def snap():
    c = st._conn()
    n = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    vs = c.execute("SELECT COUNT(*) FROM video_stats").fetchone()[0]
    ac = c.execute("SELECT COUNT(*) FROM auto_commented").fetchone()[0]
    oc = c.execute("SELECT COUNT(*) FROM own_comments").fetchone()[0]
    op = c.execute("SELECT COUNT(*) FROM own_comment_pending").fetchone()[0]
    dl = c.execute("SELECT COUNT(*) FROM delete_logs").fetchone()[0]
    c.close()
    return dict(comments=n, video_stats=vs, auto_commented=ac, own_comments=oc,
                pending=op, delete_logs=dl)


before = snap()
print("迁移前:", before)
res = st.migrate_comments_to_finder(fid_map)
print("迁移结果:", res)
after = snap()
print("迁移后:", after)
assert after["comments"] == before["comments"], "评论行数不能变(丢了就是 bug)"
assert after["own_comments"] == before["own_comments"], "自己评论登记行数不能变"

c = st._conn()
rows = c.execute("""SELECT CASE WHEN finder_id LIKE 'acct:%' THEN 'acct兜底' ELSE '真视频号' END,
                           COUNT(*) FROM comments GROUP BY 1""").fetchall()
print("评论归属分布:", rows)
orphan = c.execute("SELECT account_id, COUNT(*) FROM comments WHERE finder_id LIKE 'acct:%' "
                   "GROUP BY account_id").fetchall()
print("未映射卡片的评论:", orphan)
print("有评论的视频号数:", c.execute(
    "SELECT COUNT(DISTINCT finder_id) FROM comments").fetchone()[0])
dup = c.execute("""SELECT finder_id, export_id, COUNT(*) c2 FROM video_stats
                   GROUP BY finder_id, export_id HAVING c2 > 1""").fetchall()
print("video_stats 重复主键:", dup)
c.close()
finders = st.comment_finders()
print("归属视图前 5:", [(f["finder_id"][:12] + "…", f["comments"]) for f in finders[:5]])
print("干跑完成,数据校验通过")
