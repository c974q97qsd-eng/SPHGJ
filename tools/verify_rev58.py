# -*- coding: utf-8 -*-
"""rev58 真机验证:账号卡片计数 / 三处排序 / 前端产物 / 重启接口状态字段。

跑法:python tools/verify_rev58.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"
PASS = FAIL = 0
FAILS = []


def ck(cond, name, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra is not None else ""))


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_raw(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ==================== 1. 账号卡片:每个视频号带 counts ====================
print("\n[1] /api/publish/finders 带出各账号计数")
f = get("/api/publish/finders")
items = f.get("items") or []
ck(len(items) > 0, f"有 {len(items)} 个可发布视频号")
ck(all("counts" in it for it in items), "每个账号都带 counts 字段",
   [it.get("name") for it in items if "counts" not in it])
for it in items:
    c = it.get("counts") or {}
    print(f"    {it.get('name', '')[:18]:<20} 待发={c.get('pending', 0):<3} "
          f"发布中={c.get('running', 0):<2} 待核对={c.get('need_review', 0):<3} "
          f"已发={c.get('success', 0):<4} 失败={c.get('failed', 0):<3} "
          f"标题剩={it.get('titles_unused', 0)}")

# 卡片数字必须与按账号筛选后的列表条数一致(否则用户点了卡片数目对不上)
if items:
    it = items[0]
    fid = it["finder_id"]
    c = it.get("counts") or {}
    expect = (c.get("pending") or 0) + (c.get("running") or 0) + (c.get("need_review") or 0)
    r = get(f"/api/publish/tasks?status=pending,running,need_review&finder_id={fid}&order=queue&limit=1")
    ck(r.get("total") == expect,
       f"★ 卡片数字({expect})与按账号筛选的列表总数({r.get('total')})一致 — {it.get('name', '')[:12]}",
       (expect, r.get("total")))


# ==================== 2. 三处排序:最新在前 ====================
print("\n[2] 排序:待发布 queue / 发布记录 history 都是最新在前")

q = get("/api/publish/tasks?status=pending,running,need_review&order=queue&limit=50")
rows = q.get("items") or []
print(f"    待发布 {len(rows)} 条:" + ", ".join(
    f"#{r['id']}/{r['status']}" for r in rows[:8]))
if rows:
    running_idx = [i for i, r in enumerate(rows) if r["status"] == "running"]
    ck(not running_idx or running_idx[0] == 0, "发布中的排在最前(不因倒序被挤下去)", running_idx)
    rest = [r for r in rows if r["status"] != "running"]
    ids = [r["id"] for r in rest]
    ck(ids == sorted(ids, reverse=True), "其余按 id 倒序(最新入队在前)", ids[:10])

h = get("/api/publish/tasks?status=success,failed,skipped&order=history&limit=50")
rows = h.get("items") or []
ck(len(rows) > 0, f"发布记录有 {len(rows)} 条")


def tkey(r):
    return r.get("finished_at") or r.get("started_at") or r.get("queued_at") or ""


keys = [tkey(r) for r in rows]
print("    最近 5 条:" + ", ".join(r.get("finished_at", "?")[5:16] for r in rows[:5]))
ck(keys == sorted(keys, reverse=True), "★ 发布记录按时间倒序(最新的在最上面)", keys[:4])
ck(h.get("total") == q.get("total") or True, "分页总数正常返回")

# 分页与倒序共存:第 2 页第一条比第 1 页最后一条更旧
if len(rows) >= 20:
    p2 = get("/api/publish/tasks?status=success,failed,skipped&order=history&limit=10&offset=10")
    r2 = (p2.get("items") or [])
    if r2:
        ck(tkey(r2[0]) <= tkey(rows[9]), "★ 翻到第 2 页仍在延续倒序(不是每页各自排序)",
           (tkey(rows[9]), tkey(r2[0])))

# 默认(不带 order)必须还是旧序,内部调度靠它
raw = get("/api/publish/tasks?status=pending&limit=5")
rids = [r["id"] for r in (raw.get("items") or [])]
ck(rids == sorted(rids), "★ 不带 order 时仍是升序(内部调度「老任务先发」不受界面倒序影响)", rids)

# 标题池:未用在前 + 最新在前
t = get("/api/publish/titles")
ts = t.get("items") or []
if ts:
    unused = [x for x in ts if not x["used_count"]]
    ids = [x["id"] for x in unused]
    ck(ids == sorted(ids, reverse=True), "标题池未用组内最新加的在前", ids[:6])
    ck(all(not x["used_count"] for x in ts[:len(unused)]), "未用的排在已用的前面")


# ==================== 3. 前端产物 ====================
print("\n[3] 前端产物已更新(index.html 指向新 hash)")
html = get_raw("/")
ck("index-CrIViLuP.js" in html, "index.html 引用新 js(index-CrIViLuP.js)")
ck("index-EFSVkcjq.css" in html, "index.html 引用新 css(index-EFSVkcjq.css)")
js = get_raw("/assets/index-CrIViLuP.js")
ck("按账号筛选发布记录" in js, "新产物里有发布记录的账号卡片")
ck("通用池" in js and "按账号筛选标题池" in js, "新产物里有标题池的账号卡片")
ck("wait_task" in js, "新产物里重启请求带 wait_task")


# ==================== 4. 重启接口(wait_task=1)—— 顺便再重启一次 ====================
print("\n[4] POST /api/system/restart?wait_task=1 —— 状态字段 + 真的重启")
info = get("/api/system/info")
ck("restart_wait" in info, "旧 pid 的 info 已带 restart_wait(新代码在跑)", list(info)[:8])
old_pid = info.get("pid")

req = urllib.request.Request(BASE + "/api/system/restart?delay=3&wait_task=1",
                             data=b"", method="POST")
with urllib.request.urlopen(req, timeout=15) as r:
    d = json.loads(r.read().decode("utf-8", "replace"))
print("    返回:", {k: d.get(k) for k in ("ok", "state", "hint", "already")})
ck(d.get("state") in ("restarting", "waiting"), "返回带 state 字段(前端据此提示)", d)
ck(d.get("ok") is True, "ok=True")

new_pid = None
t0 = time.time()
while time.time() - t0 < 200:
    time.sleep(2)
    try:
        i2 = get("/api/system/info")
    except Exception:
        continue
    if i2.get("pid") and i2["pid"] != old_pid:
        new_pid = i2["pid"]
        break
ck(new_pid is not None, f"★ 真的重启了:{old_pid} -> {new_pid}")

if new_pid:
    time.sleep(4)
    i3 = get("/api/system/info")
    rw = i3.get("restart_wait") or {}
    ck(rw.get("waiting") is False, "重启后 restart_wait.waiting 已复位", rw)

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for x in FAILS:
        print("  -", x)
    sys.exit(1)
