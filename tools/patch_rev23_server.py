# -*- coding: utf-8 -*-
"""rev23-server: 作品归属改视频号 finder_id;新增 /api/posts/finders;批量按视频号找登录态。"""
import io, sys

P = r"D:\SPHGJ\backend\server.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# ---------- 1) 启动时一次性迁移(幂等) ----------
old = '''storage = Storage(os.path.join(ROOT, config.get("db_path", "./data/comments.db")) or os.path.join(ROOT, "data/comments.db"))
manager = AccountManager(config, storage, emit=hub.emit)'''
new = '''storage = Storage(os.path.join(ROOT, config.get("db_path", "./data/comments.db")) or os.path.join(ROOT, "data/comments.db"))
# rev23: 作品归属主键从「登录卡片 account_id」迁到「视频号 finder_id」(幂等,首次启动执行一次)
try:
    _fid_map = {}
    for _a in config.get("accounts", []):
        _fid = (_a.get("_log_finder_id") or _a.get("locked_finder_id") or "").strip()
        if _fid and _a.get("id"):
            _fid_map[_a["id"]] = _fid
    storage.migrate_posts_to_finder(_fid_map)
except Exception as _e:
    logger.warning(f"[启动] 作品归属迁移失败(不影响启动,下次再试): {_e}")
manager = AccountManager(config, storage, emit=hub.emit)'''
assert s.count(old) == 1, "migration hook"
s = s.replace(old, new, 1)

# ---------- 2) 视频号索引辅助函数 ----------
old = '''class PostsJob:
    """作品刷新/批量操作后台任务(同一时间只允许一个,避免抢浏览器)。"""'''
new = '''# ===================== 作品归属:视频号(finder_id)= 主键 =====================
# 作品是「视频号」的资产,不是「登录卡片」的。卡片只提供登录态(抓取/写操作通道),
# 作品行里的 finder_id 才是归属;卡片删了/换号了,作品仍然属于那个视频号。
def _resolve_finder(account_id):
    """登录卡片 id -> 视频号 finder_id(以配置里当前的登录身份为准)。"""
    a = next((x for x in config.get("accounts", []) if x.get("id") == account_id), None)
    if not a:
        return ""
    return (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()


def _finder_label(fid):
    return "视频号 " + (fid or "")[-8:] if fid else "未知视频号"


def _finder_index():
    """finder_id -> {name, account_id, account_ids, logged_in}(同视频号多卡片时优先已登录的)。"""
    idx = {}
    for a in config.get("accounts", []):
        fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()
        if not fid:
            continue
        w = manager.get_worker(a["id"])
        logged = bool(w and w.logged_in)
        e = idx.get(fid)
        if e is None:
            idx[fid] = {"finder_id": fid, "name": a.get("name") or a["id"],
                        "account_id": a["id"], "account_ids": [a["id"]],
                        "logged_in": logged, "_logged_pick": logged}
            continue
        e["account_ids"].append(a["id"])
        e["logged_in"] = e["logged_in"] or logged
        if logged and not e["_logged_pick"]:
            e.update(name=a.get("name") or a["id"], account_id=a["id"], _logged_pick=True)
    for e in idx.values():
        e.pop("_logged_pick", None)
    return idx


def _all_finders():
    """所有已知视频号:配置里的 + 作品库里的(含卡片已删除的历史视频号)。"""
    ids = []
    seen = set()
    for fid in _finder_index().keys():
        if fid not in seen:
            seen.add(fid); ids.append(fid)
    for row in storage.posts_finders():
        fid = row.get("finder_id") or ""
        if fid and fid not in seen:
            seen.add(fid); ids.append(fid)
    return ids


def _worker_for_finder(fid):
    """找当前登录着该视频号的 worker(写操作通道)。"""
    for _aid, w in manager.workers.items():
        if (w.account.get("_log_finder_id") or "").strip() == fid and w.logged_in:
            return w
    return None


class PostsJob:
    """作品刷新/批量操作后台任务(同一时间只允许一个,避免抢浏览器)。"""'''
assert s.count(old) == 1, "helpers anchor"
s = s.replace(old, new, 1)

# ---------- 3) 刷新:账本按 finder_id 记 ----------
old = '''            else:
                st["status"] = "ok"
                storage.set_post_fetch_meta(
                    aid, last_refresh=datetime.now().isoformat(),'''
new = '''            else:
                st["status"] = "ok"
                # 账本按视频号记账:同一视频号换卡片登录不用重跑全量
                storage.set_post_fetch_meta(
                    (w.account.get("_log_finder_id") or "").strip() or aid,
                    account_id=aid, last_refresh=datetime.now().isoformat(),'''
assert s.count(old) == 1, "refresh meta"
s = s.replace(old, new, 1)

# ---------- 4) 批量:按视频号分组 + 按视频号找登录态 ----------
old = '''async def _do_posts_batch(action, items):
    st_all = posts_job.state
    groups = {}
    for it in items:
        groups.setdefault(it.account_id, []).append(it.object_id)
    st_all["accounts"] = {aid: {"status": "pending", "updated": 0, "skipped": 0, "failed": []}
                          for aid in groups}
    st_all["total"] = len(items)
    VIS = {"hide": 3, "unhide": 1}
    for aid, oids in groups.items():
        st = st_all["accounts"][aid]
        w = manager.get_worker(aid)
        if not w or not w.logged_in:'''
new = '''async def _do_posts_batch(action, items):
    st_all = posts_job.state
    groups = {}
    for it in items:
        # 归属键是视频号;旧前端只传 account_id 时按卡片解析成视频号
        fid = (getattr(it, "finder_id", "") or "").strip() or _resolve_finder(it.account_id)
        if not fid:
            continue
        groups.setdefault(fid, []).append(it.object_id)
    if not groups:
        st_all["failed"].append({"error": "找不到作品归属的视频号,请刷新后重试"})
        return
    st_all["accounts"] = {fid: {"status": "pending", "updated": 0, "skipped": 0, "failed": []}
                          for fid in groups}
    st_all["total"] = sum(len(v) for v in groups.values())
    VIS = {"hide": 3, "unhide": 1}
    for fid, oids in groups.items():
        st = st_all["accounts"][fid]
        w = _worker_for_finder(fid)
        if not w:'''
assert s.count(old) == 1, "batch head"
s = s.replace(old, new, 1)

old = '''            st["status"] = "not_logged_in"
            for oid in oids:
                st["failed"].append({"object_id": oid, "error": "账号未启动或未登录"})
                st_all["done"] += 1
            continue'''
new = '''            st["status"] = "not_logged_in"
            for oid in oids:
                st["failed"].append({"object_id": oid,
                                     "error": "该视频号当前没有已登录的账号卡片,请先启动/扫码登录"})
                st_all["done"] += 1
            continue'''
assert s.count(old) == 1, "batch not logged"
s = s.replace(old, new, 1)

old = '''                p = storage.get_post(aid, oid)'''
new = '''                p = storage.get_post(fid, oid)'''
assert s.count(old) == 1, "batch get_post"
s = s.replace(old, new, 1)

for a_, b_ in (
    ('storage.update_post_flags(aid, oid, visible_type=VIS[action])',
     'storage.update_post_flags(fid, oid, visible_type=VIS[action])'),
    ('storage.update_post_flags(aid, oid, sticky_op=2)',
     'storage.update_post_flags(fid, oid, sticky_op=2)'),
    ('storage.update_post_flags(aid, oid, sticky_op=0)',
     'storage.update_post_flags(fid, oid, sticky_op=0)'),
):
    assert s.count(a_) == 1, "flags " + a_
    s = s.replace(a_, b_, 1)

old = '''        await hub.emit("posts_batch_progress", {"account_id": aid, **st})'''
new = '''        await hub.emit("posts_batch_progress", {"finder_id": fid, **st})'''
assert s.count(old) == 1, "batch emit"
s = s.replace(old, new, 1)

# ---------- 5) 查询接口整体重写 ----------
i0 = s.index('@app.get("/api/posts")\nasync def get_posts(')
i1 = s.index('@app.get("/api/posts/job")')
NEW_EP = '''@app.get("/api/posts")
async def get_posts(
    finder_id: Optional[str] = None,
    account_id: Optional[str] = None,   # 兼容旧调用:按登录卡片解析成视频号
    q: Optional[str] = None,
    visible: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: str = "create_time",
    order: str = "desc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """作品查询(本地库,零官方请求)。按视频号归属筛选;visible: public/follow/hidden/sticky。"""
    fid = (finder_id or "").strip()
    if not fid and account_id:
        fid = _resolve_finder(account_id)
    items, total = storage.query_posts(
        finder_id=fid or None, q=q, visible=visible,
        date_from=date_from, date_to=date_to, sort=sort, order=order,
        limit=limit, offset=offset)
    idx = _finder_index()
    name_map = {a["id"]: a.get("name", a["id"]) for a in config.get("accounts", [])}
    for it in items:
        e = idx.get(it["finder_id"]) or {}
        # 归属名以视频号为准;卡片名只作「谁在抓」的追溯
        it["finder_name"] = e.get("name") or _finder_label(it["finder_id"])
        it["account_name"] = name_map.get(it.get("account_id") or "",
                                          it.get("account_id") or "")
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/posts/finders")
async def get_posts_finders():
    """作品归属视图:有哪些视频号、各多少作品、当前由哪张卡片承载、抓取账本。

    这是作品管理的主维度 —— 视频号(finder_id),不是登录卡片。
    卡片删掉/换号后,视频号与它的作品仍然在这里(只会显示「未绑定卡片」)。
    """
    idx = _finder_index()
    rows = {r["finder_id"]: r for r in storage.posts_finders()}
    out = []
    for fid in _all_finders():
        e = idx.get(fid) or {}
        r = rows.get(fid) or {}
        out.append({
            "finder_id": fid,
            "name": e.get("name") or _finder_label(fid),
            "account_id": e.get("account_id") or r.get("account_id") or "",
            "account_ids": e.get("account_ids") or [],
            "logged_in": bool(e.get("logged_in")),
            "bound": bool(e),
            "works": r.get("works", 0),
            "hidden": r.get("hidden", 0),
            "sticky": r.get("sticky", 0),
            "last_create": r.get("last_create"),
            "last_update": r.get("last_update"),
            "meta": storage.get_post_fetch_meta(fid),
        })
    out.sort(key=lambda x: (-(x["works"] or 0), x["name"]))
    return {"items": out}


@app.get("/api/posts/meta")
async def get_posts_meta():
    """各视频号的抓取账本(上次刷新/今日请求次数),键 = finder_id。"""
    return {"meta": {fid: storage.get_post_fetch_meta(fid) for fid in _all_finders()}}


@app.post("/api/posts/refresh")
async def posts_refresh(body: schemas.PostsRefreshBody):
    """手动刷新作品(增量,后台任务)。account_id 空 = 全部已登录账号。"""
    if body.account_id:
        aids = [body.account_id]
    else:
        aids = [aid for aid, w in manager.workers.items() if w.logged_in]
    if not aids:
        raise HTTPException(400, "没有已登录账号,请先启动引擎")
    ok, msg = await posts_job.start("refresh", _do_posts_refresh(list(aids)))
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "accounts": aids}


@app.post("/api/posts/batch")
async def posts_batch(body: schemas.PostsBatchBody):
    """批量操作:hide/unhide/sticky/unsticky(后台任务,写操作逐条节流)。items 以 finder_id 归属。"""
    if body.action not in ("hide", "unhide", "sticky", "unsticky"):
        raise HTTPException(400, "action 仅支持 hide/unhide/sticky/unsticky")
    if not body.items:
        raise HTTPException(400, "未选择作品")
    ok, msg = await posts_job.start("batch", _do_posts_batch(body.action, body.items))
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "total": len(body.items)}


'''
s = s[:i0] + NEW_EP + s[i1:]

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = [
    ("迁移钩子", "storage.migrate_posts_to_finder(_fid_map)" in s),
    ("_finder_index", "def _finder_index()" in s),
    ("_worker_for_finder", "def _worker_for_finder(fid)" in s),
    ("finders 接口", '@app.get("/api/posts/finders")' in s),
    ("批量按视频号分组", "groups.setdefault(fid, []).append(it.object_id)" in s),
    ("批量取作品按视频号", "p = storage.get_post(fid, oid)" in s),
    ("刷新账本按视频号", "account_id=aid, last_refresh=datetime.now().isoformat()," in s),
    ("旧按卡片取作品已去", "storage.get_post(aid, oid)" not in s),
    ("query 传 finder_id", "finder_id=fid or None" in s),
]
bad = [n for n, ok in chk if not ok]
print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
sys.exit(0 if not bad else 1)
