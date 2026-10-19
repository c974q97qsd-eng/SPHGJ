# -*- coding: utf-8 -*-
"""确认刚才那条是否真的发到视频号:刷新作品列表 + 查最新作品。"""
import json
import time
import urllib.request

API = "http://192.168.1.27:8712"


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def get(path):
    return json.loads(urllib.request.urlopen(API + path, timeout=120).read())


print("== 触发刷新(免税仓) ==")
print(post("/api/posts/refresh", {"finder_id": "", "account_id": "uspoloassn2"}))

for i in range(20):
    time.sleep(6)
    try:
        job = get("/api/posts/job")
    except Exception:
        job = {}
    st = (job.get("status") or job.get("state") or "")
    if st in ("done", "idle", "finished", ""):
        if i > 3:
            break
print("job:", json.dumps(job, ensure_ascii=False)[:300])

print("\n== 免税仓作品首屏 ==")
d = get("/api/posts?limit=6&finder_id=")
items = d.get("items") or []
for it in items[:6]:
    print(f"  {(it.get('create_time') or '')[:16]} | {it.get('finder_name') or it.get('account_id')} | {(it.get('desc') or it.get('title') or '')[:40]!r}")
print("total", d.get("total"))
