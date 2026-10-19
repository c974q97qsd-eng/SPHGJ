# -*- coding: utf-8 -*-
"""rev24 归属迁移自测(离线,不碰真实库)。

构造一个 rev23 之前的老库(评论域主键含 account_id),模拟三种真实场景:
  1) 同一视频号挂过两张卡片 A1/A2 -> 评论/快照应当合并到同一个 finder
  2) 配置里已删除的历史卡片 X -> 用 acct:X 兜底,数据一条不丢
  3) 同一评论被真视频号再抓到 -> finder_id 自愈,从 acct: 变回真值
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, r"D:\SPHGJ")
from backend.storage import Storage  # noqa: E402

F1 = "FinderAAA111"
DB = os.path.join(tempfile.mkdtemp(prefix="rev24_"), "t.db")

# ---------------- 老库(rev23 之前的 schema)----------------
c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE comments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT, export_id TEXT, comment_id TEXT UNIQUE, author_id TEXT,
    nickname TEXT, content TEXT, head_url TEXT, create_time INTEGER,
    like_count INTEGER, read_flag INTEGER, replied INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0, raw TEXT, fetched_at TEXT);
CREATE TABLE video_stats(account_id TEXT, export_id TEXT, comment_count INTEGER,
    updated_at TEXT, PRIMARY KEY(account_id, export_id));
CREATE TABLE auto_commented(account_id TEXT, export_id TEXT, comment_id TEXT,
    commented_at TEXT, PRIMARY KEY(account_id, export_id));
CREATE TABLE own_comments(account_id TEXT, comment_id TEXT UNIQUE, via TEXT, created_at TEXT);
CREATE TABLE own_comment_pending(id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT, export_id TEXT, content TEXT, via TEXT, created_at TEXT);
CREATE TABLE delete_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT,
    comment_id TEXT, export_id TEXT, nickname TEXT, content TEXT, keyword TEXT, deleted_at TEXT);
""")
c.executemany("INSERT INTO comments(account_id,export_id,comment_id,nickname,content,create_time,"
              "deleted,raw,fetched_at) VALUES(?,?,?,?,?,?,0,'{}',?)",
              [("A1", "e1", "c1", "客户甲", "有38码吗", 1000, "2026-10-01T00:00:00"),
               ("A2", "e1", "c2", "客户乙", "发什么快递", 1001, "2026-10-01T00:00:00"),
               ("X", "e9", "cx", "历史客户", "老卡片抓的", 900, "2026-09-01T00:00:00")])
c.executemany("INSERT INTO video_stats VALUES(?,?,?,?)",
              [("A1", "e1", 5, "2026-10-01T00:00:00"), ("A2", "e1", 7, "2026-10-02T00:00:00"),
               ("X", "e9", 2, "2026-09-01T00:00:00")])
c.executemany("INSERT INTO auto_commented VALUES(?,?,?,?)",
              [("A1", "e2", "p1", "2026-10-01T00:00:00"), ("A2", "e2", "p2", "2026-10-03T00:00:00")])
c.execute("INSERT INTO own_comments VALUES('A1','own1','auto_reply','2026-10-01T00:00:00')")
c.execute("INSERT INTO own_comment_pending(account_id,export_id,content,via,created_at) "
          "VALUES('A2','e1','有38码的 亲','auto_reply','2026-10-10T00:00:00')")
c.execute("INSERT INTO delete_logs(account_id,comment_id,export_id,nickname,content,keyword,deleted_at) "
          "VALUES('X','cx','e9','历史客户','加微信','微信','2026-09-02T00:00:00')")
c.commit()
c.close()

# ---------------- 打开 ==_init 结构性迁移(acct: 兜底)----------------
st = Storage(DB)
conn = st._conn()
cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in
        ("comments", "video_stats", "auto_commented", "own_comments",
         "own_comment_pending", "delete_logs")}
for t, cs in cols.items():
    assert "finder_id" in cs, f"{t} 缺 finder_id 列"
assert conn.execute("SELECT COUNT(*) FROM video_stats").fetchone()[0] == 3, "结构迁移不应丢行"
print("步骤1 结构迁移 OK(全部表补上 finder_id,行数不变)")

# ---------------- 配置映射迁移 ----------------
res = st.migrate_comments_to_finder({"A1": F1, "A2": F1})
print("步骤2 配置迁移:", res)
conn = st._conn()
by_cid = {r[0]: r[1] for r in conn.execute("SELECT comment_id, finder_id FROM comments")}
assert by_cid["c1"] == F1 and by_cid["c2"] == F1, f"同视频号两张卡片应归到同一个 finder: {by_cid}"
assert by_cid["cx"] == "acct:X", f"未映射的历史卡片应兜底 acct:X: {by_cid}"
vs = conn.execute("SELECT finder_id,export_id,comment_count,account_id FROM video_stats "
                  "ORDER BY finder_id").fetchall()
assert len(vs) == 2, f"同视频号 e1 应合并成 1 行: {vs}"
assert (F1, "e1", 7) == vs[0][:3], f"合并应保留更大的评论数: {vs}"
assert vs[0][3] == "A2", f"合并应保留更新的来源卡片: {vs}"
ac = conn.execute("SELECT finder_id,export_id,comment_id FROM auto_commented").fetchall()
assert ac == [(F1, "e2", "p2")], f"auto_commented 应合并并保留最新: {ac}"
assert conn.execute("SELECT finder_id FROM own_comments WHERE comment_id='own1'").fetchone()[0] == F1
assert conn.execute("SELECT finder_id FROM own_comment_pending").fetchone()[0] == F1
assert conn.execute("SELECT finder_id FROM delete_logs").fetchone()[0] == "acct:X"
conn.close()
print("步骤3 归属迁移 OK(合并去重 + 兜底不丢)")

# ---------------- 查询/写入按视频号 ----------------
items, total = st.query_comments(finder_id=F1)
assert total == 2 and all(i["finder_id"] == F1 for i in items), (total, items)
assert st.query_comments(account_id="X")[1] == 1, "account_id 兼容筛选仍可用"
state = st.get_video_fetch_state(F1, "e1")
assert state == dict(seen=True, cc=7, has_comments=True), state
assert st.set_video_comment_count(F1, "e1", 3) == 7, "脏值 0/回退必须被单调性保护挡住"
assert st.set_video_comment_count(F1, "e1", 9) == 9, "正常增长要能写入"
assert st.get_video_fetch_state(F1, "e1")["cc"] == 9
print("步骤4 按视频号读写 OK")

# ---------------- 自己评论:登记 / 待确认补登 ----------------
st.mark_own_comment(F1, "own2", "auto_comment", content="新款到货", export_id="e1", account_id="A2")
assert st.pending_own_comment_count(F1) == 1, "同一视频号的待确认条数应按 finder 统计"
# 造一条同视频号、同内容、没登记过的评论 -> 补登应命中
st.upsert_comment(F1, "e1", {"commentId": "c3", "commentNickname": "客户丙",
                             "commentContent": "有38码的 亲", "commentCreatetime": 1002}, "A1")
n = st.reconcile_own_comments(F1)
assert n == 1, f"待确认应补登 1 条: {n}"
assert st.pending_own_comment_count(F1) == 0
print("步骤5 发出评论登记/补登 OK")

# ---------------- 删除记录按视频号 ----------------
st.log_delete(F1, "c9", "客户丁", "刷屏内容", "微信", "e1", "A1")
logs, n = st.query_delete_logs(finder_id=F1)
assert n == 1 and logs[0]["finder_id"] == F1 and logs[0]["keyword"] == "微信", logs
st.clear_delete_logs(finder_id=F1)
assert st.query_delete_logs(finder_id=F1)[1] == 0
assert st.query_delete_logs()[1] == 1, "只清指定视频号,历史 acct 记录要留着"
print("步骤6 删除记录按视频号 OK")

# ---------------- 归一视图 + finder_id 自愈 ----------------
finders = {r["finder_id"]: r["comments"] for r in st.comment_finders()}
assert finders.get(F1) == 3, finders          # c1 c2 c3
assert finders.get("acct:X") == 1, finders
st.upsert_comment(F1, "e9", {"commentId": "cx", "commentContent": "真值覆盖",
                             "commentNickname": "历史客户", "commentCreatetime": 900}, "A1")
assert st.get_comment("cx")["finder_id"] == F1, "旧 acct 值应被真视频号自愈覆盖"
print("步骤7 归属视图 + 自愈 OK")

# ---------------- 幂等 ----------------
res2 = st.migrate_comments_to_finder({"A1": F1, "A2": F1})
assert res2["merged"] == 0, f"二次迁移不应再合并: {res2}"
assert st.query_comments(finder_id=F1)[1] == 4, "c1/c2/c3 + 自愈后的 cx 都该在 F1 下"
print("步骤8 幂等 OK")
print("ALL PASS", DB)
