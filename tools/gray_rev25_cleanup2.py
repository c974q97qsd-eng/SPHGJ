# -*- coding: utf-8 -*-
"""清掉灰度时加的两条测试标题(专属池,上一脚本只查了通用池)。"""
import json, urllib.request

BASE = "http://192.168.1.27:8712"


def req(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http": e.code, "detail": e.read().decode()[:300]}


items = req("/api/publish/titles").get("items") or []
print("标题总数", len(items))
for it in items:
    print("  ", it["id"], repr(it["content"])[:50], "used" if it["used_count"] else "unused", "pool=" + (it["finder_id"] or "通用"))
    if "老爹鞋上脚真的一眼出片" in it["content"] or "秋冬配色太耐看了" in it["content"]:
        print("    -> 删除", req(f"/api/publish/titles/{it['id']}", "DELETE"))

items = req("/api/publish/titles").get("items") or []
print("清理后标题数", len(items))
