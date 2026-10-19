# -*- coding: utf-8 -*-
"""rev24 线上校验:评论归属是否已全部按视频号,接口是否通。"""
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request

BASE = "http://192.168.1.27:8712"
UNC = r"\\Sphgj\sphgj"


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=40) as r:
        return json.loads(r.read())


print("== 1. 归属视图 /api/comments/finders ==")
d = api("/api/comments/finders")
items = d["items"]
print(f"  视频号 {len(items)} 个")
tot = 0
for it in items:
    tot += it["comments"]
    print(f"  {it['name'][:22]:24s} 评论 {it['comments']:5d}  登录={it['logged_in']}  卡片={it['account_id']}")
print(f"  评论合计 {tot}")
orphan = [it for it in items if (it["finder_id"] or "").startswith("acct:")]
print(f"  仍为旧卡片兜底: {len(orphan)} 个 {[o['finder_id'] for o in orphan]}")

print("== 2. 列表接口带 finder_id ==")
p = api("/api/comments?limit=3")
print("  total:", p["total"])
for it in p["items"]:
    print(f"  finder_id={(it.get('finder_id') or '')[:16]}…  account_id={it['account_id']:14s} {str(it['content'])[:16]}")
assert all(it.get("finder_id") for it in p["items"]), "列表项缺 finder_id"

if items:
    fid = items[0]["finder_id"]
    q = urllib.parse.quote(fid)
    p2 = api(f"/api/comments?finder_id={q}&limit=1")
    print(f"  按视频号筛选 {items[0]['name'][:16]} -> total={p2['total']} (视图值 {items[0]['comments']})")
    assert p2["total"] == items[0]["comments"], "按视频号筛选结果与归属视图不一致"

print("== 3. 删除记录带视频号名 ==")
lg = api("/api/auto-delete/logs?limit=3")
print("  total:", lg["total"])
for it in lg["items"]:
    print(f"  finder={(it.get('finder_id') or '')[:14]}… name={it.get('finder_name')} 关键字={it['keyword']}")

print("== 4. 母本库结构 ==")
db = os.path.join(UNC, "data", "sphgj.db")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
for t in ("comments", "video_stats", "auto_commented", "own_comments",
          "own_comment_pending", "delete_logs"):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
    ok = "finder_id" in cols
    print(f"  {'OK ' if ok else 'BAD'} {t:20s} 有 finder_id={ok}")
    assert ok, f"{t} 缺 finder_id"

rows = c.execute("""SELECT CASE WHEN finder_id LIKE 'acct:%' THEN 'acct兜底' ELSE '真视频号' END k,
                           COUNT(*) FROM comments GROUP BY k""").fetchall()
print("  评论归属分布:", rows)
assert all(k == "真视频号" for k, _n in rows), f"仍有兜底归属: {rows}"
dup = c.execute("""SELECT COUNT(*) FROM (SELECT finder_id,export_id FROM video_stats
                   GROUP BY finder_id,export_id HAVING COUNT(*)>1)""").fetchone()[0]
print("  video_stats 重复主键:", dup)
assert dup == 0, "video_stats 仍有重复主键"
print("  comments 总行数:", c.execute("SELECT COUNT(*) FROM comments").fetchone()[0])
print("  video_stats 行数:", c.execute("SELECT COUNT(*) FROM video_stats").fetchone()[0])
print("  delete_logs 行数:", c.execute("SELECT COUNT(*) FROM delete_logs").fetchone()[0])
c.close()

print("== 5. 前端包 ==")
with urllib.request.urlopen(BASE + "/", timeout=20) as r:
    html = r.read().decode("utf-8", "ignore")
assert "index-DSinavzX.js" in html, "首页未指向新 bundle"
print("  index-DSinavzX.js OK")
print("ALL PASS")
