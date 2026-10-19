# -*- coding: utf-8 -*-
"""清理 rev25 灰度测试残留:任务 #1 / 测试视频 / 测试标题。"""
import json, os, sys, urllib.request

BASE = "http://192.168.1.27:8712"
UNC = r"\\Sphgj\sphgj"


def req(path, method="GET", body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http": e.code, "detail": e.read().decode()[:300]}


# 1) 删测试任务(失败/待发的都清)
for st in ("pending,running,need_review", "failed,skipped,success"):
    for it in (req(f"/api/publish/tasks?status={st}").get("items") or []):
        if "测试素材" in it["file_name"] or it["id"] == 1:
            print("删除任务", it["id"], it["file_name"], req(f"/api/publish/tasks/{it['id']}", "DELETE"))

# 2) 测试视频移出 inbox
src = os.path.join(UNC, r"data\publish_inbox\优选仓\测试素材_老爹鞋.mp4")
dst_dir = os.path.join(UNC, "data", "_pubtest")
if os.path.isfile(src):
    try:
        os.rename(src, os.path.join(dst_dir, "测试素材_老爹鞋.mp4"))
        print("测试视频已移出 inbox -> data/_pubtest/")
    except Exception as e:
        print("[WARN] 移动失败:", e)

# 3) 清测试标题(我加的两条)
for it in (req(f"/api/publish/titles?finder_id=").get("items") or []):
    if "老爹鞋上脚真的一眼出片" in it["content"] or "秋冬配色太耐看了" in it["content"]:
        print("删除标题", it["id"], req(f"/api/publish/titles/{it['id']}", "DELETE"))

print("\n=== 清理后 ===")
s = req("/api/publish/status")
print("pending", s["pending"], "running", s["running"], "enabled", s["enabled"])
t = req("/api/publish/tasks")
print("任务总数", t["total"])
for it in t.get("items") or []:
    print("  残留:", it["id"], it["file_name"], it["status"])
print("\ninbox 目录:")
root = os.path.join(UNC, r"data\publish_inbox")
for d in sorted(os.listdir(root)):
    p = os.path.join(root, d)
    if os.path.isdir(p):
        vids = [f for f in os.listdir(p) if f.lower().endswith((".mp4", ".mov", ".m4v"))]
        print(f"  {d:12s} {len(vids)} 个视频" + (f"  {vids}" if vids else ""))
