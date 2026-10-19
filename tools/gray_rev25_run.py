# -*- coding: utf-8 -*-
"""rev25 灰度:加标题 + 扫描 + 演练(dry_run)全链路验证。只用标准库,避免 shell 转义坑。"""
import json, sys, time, urllib.request, urllib.parse

BASE = "http://192.168.1.27:8712"


def req(path, method="GET", body=None, timeout=360):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"__http": e.code, "detail": e.read().decode("utf-8")[:400]}


# 1) 找到优选仓 finder_id
f = req("/api/publish/finders")
fid = next((x["finder_id"] for x in f.get("items", []) if "优选仓" in x["name"]), "")
assert fid, f"没找到优选仓: {f}"
print("finder_id:", fid[-20:])

# 2) 加标题
r = req("/api/publish/titles", "POST", {
    "finder_id": fid,
    "contents": "这双老爹鞋上脚真的一眼出片 #男鞋 #老爹鞋\n秋冬配色太耐看了 #穿搭 #男鞋",
})
print("加标题:", r)

# 3) 扫描(标题补上)
r = req("/api/publish/scan", "POST")
print("扫描:", r)

# 4) 看队列(没 pending 就把失败的重置回 pending)
t = req("/api/publish/tasks?status=pending")
if not (t.get("items") or []):
    failed = req("/api/publish/tasks?status=failed,need_review,skipped")
    for it in failed.get("items") or []:
        print("retry 任务#", it["id"], req(f"/api/publish/tasks/{it['id']}/retry", "POST", {}))
    t = req("/api/publish/tasks?status=pending")

for it in t.get("items") or []:
    print(f"  任务#{it['id']} {it['file_name']} | 标题={it['title']!r} | 状态={it['status']} step={it['step']}")

task = (t.get("items") or [{}])[0]
if not task.get("id"):
    print("[STOP] 队列里没有任务")
    sys.exit(1)

if "--dry" not in sys.argv:
    print("\n[dry_run] 只上传+填描述,不点发表(耗时可到 1-3 分钟)…")
    t0 = time.time()
    r = req(f"/api/publish/tasks/{task['id']}/run?dry_run=true", "POST", None, timeout=900)
    print(f"耗时 {time.time()-t0:.0f}s")
    print("结果:", json.dumps(r, ensure_ascii=False)[:1200])
    t2 = req(f"/api/publish/tasks?finder_id={urllib.parse.quote(fid)}")
    for it in t2.get("items") or []:
        print(f"  任务#{it['id']} 状态={it['status']} step={it['step']} err={it['last_error'][:160]}")
