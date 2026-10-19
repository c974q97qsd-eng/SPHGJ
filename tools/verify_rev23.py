# -*- coding: utf-8 -*-
"""重启后校验:作品表主键/数据量/封面目录/接口返回。"""
import io, json, os, sqlite3, urllib.request, urllib.parse

DB = r"\\Sphgj\sphgj\data\sphgj.db"
COV = r"\\Sphgj\sphgj\data\covers"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
print("posts 列:", [r[1] for r in c.execute("PRAGMA table_info(posts)")])
print("posts 主键:", [r[1] for r in c.execute("PRAGMA table_info(posts)") if r[5]])
print("posts 行数:", c.execute("SELECT COUNT(*) FROM posts").fetchone()[0])
print("去重作品数:", c.execute("SELECT COUNT(DISTINCT object_id) FROM posts").fetchone()[0])
print("视频号数:", c.execute("SELECT COUNT(DISTINCT finder_id) FROM posts").fetchone()[0])
print("acct: 兜底行数:", c.execute("SELECT COUNT(*) FROM posts WHERE finder_id LIKE 'acct:%'").fetchone()[0])
print("meta 列:", [r[1] for r in c.execute("PRAGMA table_info(post_fetch_meta)")])
print("meta 行数:", c.execute("SELECT COUNT(*) FROM post_fetch_meta").fetchone()[0])
print("cover_path 抽样:", [r[0] for r in c.execute("SELECT cover_path FROM posts WHERE cover_path<>'' LIMIT 2")])
print("旧式 cover_path 残留:", c.execute("SELECT COUNT(*) FROM posts WHERE cover_path LIKE '/media/covers/uspoloassn%'").fetchone()[0])
c.close()

print("\ncovers 目录:", sorted(os.listdir(COV)))

def get(path):
    with urllib.request.urlopen("http://192.168.1.27:8712" + path, timeout=30) as r:
        return json.loads(r.read().decode())

p = get("/api/posts?limit=3")
print("\n/api/posts 抽样:")
for it in p["items"]:
    print("  ", {k: it[k] for k in ("finder_id", "finder_name", "account_id", "object_id", "title")})
print("  total:", p["total"])

f0 = get("/api/posts/finders")["items"][0]
p2 = get("/api/posts?finder_id=" + urllib.parse.quote(f0["finder_id"]) + "&limit=2")
print("\n按视频号筛选:", f0["name"], "-> total", p2["total"], "首条归属", p2["items"][0]["finder_name"])
