# -*- coding: utf-8 -*-
"""rev23 迁移自测:旧库(account_id 主键)-> finder_id 主键,含封面目录搬家与幂等。"""
import os, sys, sqlite3, shutil, json, tempfile

sys.path.insert(0, r"D:\SPHGJ")
from backend.storage import Storage, cover_key

TMP = tempfile.mkdtemp(prefix="rev23_")
DB = os.path.join(TMP, "data", "t.db")
os.makedirs(os.path.dirname(DB), exist_ok=True)
COV = os.path.join(TMP, "data", "covers")

# ---- 造一个旧结构的库 ----
c = sqlite3.connect(DB)
c.execute("""CREATE TABLE posts(
    account_id TEXT, object_id TEXT,
    export_id TEXT, title TEXT, cover_url TEXT, cover_hash TEXT, cover_path TEXT,
    create_time INTEGER, read_count INTEGER, like_count INTEGER, comment_count INTEGER,
    forward_count INTEGER, fav_count INTEGER, follow_count INTEGER,
    visible_type INTEGER, sticky_op INTEGER, stat_sig TEXT,
    raw TEXT, updated_at TEXT,
    PRIMARY KEY(account_id, object_id))""")
c.execute("""CREATE TABLE post_fetch_meta(
    account_id TEXT PRIMARY KEY, last_refresh TEXT, last_pages INTEGER,
    today_date TEXT, today_requests INTEGER, full_synced INTEGER DEFAULT 0)""")
rows = [
    ("accA", "o1", "e1", "作品1", "http://c/1.jpg", "hash1", "/media/covers/accA/hash1.jpg",
     1700000000, 10, 1, 2, 0, 0, 0, 1, 0, "10:1:2", "{}", "2026-10-01T00:00:00"),
    ("accA", "o2", "e2", "作品2", "http://c/2.jpg", "hash2", "/media/covers/accA/hash2.jpg",
     1700000001, 11, 2, 3, 0, 0, 0, 3, 2, "11:2:3", "{}", "2026-10-02T00:00:00"),
    # 卡片已删除的历史账号(配置里没有)-> acct: 兜底,不能丢
    ("accB_gone", "o3", "e3", "作品3", "http://c/3.jpg", "hash3", "/media/covers/accB_gone/hash3.jpg",
     1700000002, 12, 3, 4, 0, 0, 0, 1, 0, "12:3:4", "{}", "2026-10-03T00:00:00"),
]
c.executemany("INSERT INTO posts VALUES(" + ",".join(["?"] * 19) + ")", rows)
c.execute("INSERT INTO post_fetch_meta VALUES('accA','2026-10-19T09:00:00',1,'2026-10-19',201,1)")
c.commit()
c.close()

# ---- 造旧封面目录 ----
for acc in ("accA", "accB_gone"):
    d = os.path.join(COV, acc)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "hash1.jpg"), "wb").write(b"x")

FID_A = "v2_aaaa@finder"
st = Storage(DB)                       # 旧库能正常打开(不能因缺 finder_id 列炸掉)
res = st.migrate_posts_to_finder({"accA": FID_A})
print("迁移结果:", json.dumps(res, ensure_ascii=False))
assert res["migrated"] is True
assert res["posts"] == 3, res
assert res["meta"] == 1, res
assert "accB_gone" in res["unmapped"], res

# ---- 校验 ----
items, total = st.query_posts(limit=50)
assert total == 3, total
by = {(i["finder_id"], i["object_id"]): i for i in items}
assert (FID_A, "o1") in by, by.keys()
assert by[(FID_A, "o1")]["account_id"] == "accA"
assert by[(FID_A, "o1")]["cover_path"] == "/media/covers/%s/hash1.jpg" % cover_key(FID_A), \
    by[(FID_A, "o1")]["cover_path"]
gone = [i for i in items if i["object_id"] == "o3"][0]
assert gone["finder_id"] == "acct:accB_gone", gone["finder_id"]
assert gone["cover_path"] == "/media/covers/acct_accB_gone/hash3.jpg", gone["cover_path"]

# 账本按视频号
m = st.get_post_fetch_meta(FID_A)
assert m["full_synced"] == 1 and m["account_id"] == "accA", m
assert st.get_post_fetch_meta("acct:accB_gone")["last_refresh"] is None

# 封面目录已搬
assert os.path.isdir(os.path.join(COV, cover_key(FID_A))), os.listdir(COV)
assert os.path.isfile(os.path.join(COV, cover_key(FID_A), "hash1.jpg"))
assert os.path.isdir(os.path.join(COV, "acct_accB_gone"))

# 早停语义 / 写入 / 改状态
assert st.posts_all_known(FID_A, ["o1", "o2"]) is True
assert st.posts_all_known(FID_A, ["o1", "nope"]) is False
st.upsert_posts(FID_A, [dict(object_id="o9", export_id="e9", title="新", cover_url="", cover_hash="",
                             create_time=1700000009, read_count=1, like_count=0, comment_count=0,
                             forward_count=0, fav_count=0, follow_count=0, visible_type=1,
                             sticky_op=0, raw="{}")], "accA")
assert st.get_post(FID_A, "o9")["title"] == "新"
st.update_post_flags(FID_A, "o9", visible_type=3, sticky_op=2)
p = st.get_post(FID_A, "o9")
assert p["visible_type"] == 3 and p["sticky_op"] == 2, p

# 视频号视图
fs = {r["finder_id"]: r for r in st.posts_finders()}
assert fs[FID_A]["works"] == 3, fs      # o1,o2,o9
assert fs[FID_A]["hidden"] == 2 and fs[FID_A]["sticky"] == 2, fs[FID_A]   # o2(原库) + o9(新建)

# 幂等:再迁移一次直接返回
res2 = st.migrate_posts_to_finder({"accA": FID_A})
assert res2["migrated"] is False, res2
assert st.query_posts(limit=50)[1] == 4

# 同一视频号两张卡片:作品不重复
st.upsert_posts(FID_A, [dict(object_id="o1", export_id="e1", title="作品1-卡片2再抓", cover_url="", cover_hash="",
                             create_time=1700000000, read_count=99, like_count=0, comment_count=0,
                             forward_count=0, fav_count=0, follow_count=0, visible_type=1,
                             sticky_op=0, raw="{}")], "accA2")
assert st.query_posts(limit=50)[1] == 4, "同视频号不应重复入库"
_o1 = [x for x in st.query_posts(finder_id=FID_A, limit=50)[0] if x["object_id"] == "o1"][0]
assert _o1["read_count"] == 99 and _o1["account_id"] == "accA2", _o1

print("ALL OK")
shutil.rmtree(TMP, ignore_errors=True)
