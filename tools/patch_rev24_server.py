# -*- coding: utf-8 -*-
"""rev24 补丁 3/4:服务端接口 + 请求体 schema 改按视频号归属。

- GET /api/comments 支持 finder_id
- 新增 GET /api/comments/finders(评论归属视图)
- 写操作(回复/删除/置顶/批量删除)按视频号找当前登录它的卡片,不再死认 account_id
- 删除记录 / 评论导出 都按视频号筛选
- 启动时跑 migrate_comments_to_finder
"""
import io
import sys

SERVER = r"D:\SPHGJ\backend\server.py"
SCHEMAS = r"D:\SPHGJ\backend\schemas.py"

S_PATCHES, C_PATCHES = [], []


def s(tag, old, new):
    S_PATCHES.append((tag, old, new))


def c(tag, old, new):
    C_PATCHES.append((tag, old, new))


# ============================================================ schemas
c("manual-reply", '''class ManualReply(BaseModel):
    account_id: str
    content: str = Field(..., min_length=1)''',
  '''class ManualReply(BaseModel):
    # 归属 = 视频号 finder_id(优先);account_id 保留为旧前端兼容与「找不到登录卡片」兜底
    finder_id: Optional[str] = None
    account_id: str
    content: str = Field(..., min_length=1)''')

c("delete-body", '''class DeleteCommentBody(BaseModel):
    account_id: str
    export_id: str''',
  '''class DeleteCommentBody(BaseModel):
    finder_id: Optional[str] = None
    account_id: str
    export_id: str''')

c("batch-item", '''class BatchDeleteItem(BaseModel):
    comment_id: str''',
  '''class BatchDeleteItem(BaseModel):
    finder_id: Optional[str] = None
    comment_id: str''')

c("pin-body", '''class PinCommentBody(BaseModel):
    account_id: str
    export_id: str
    op_type: int = 1  # 1 置顶 / 0 取消''',
  '''class PinCommentBody(BaseModel):
    finder_id: Optional[str] = None
    account_id: str
    export_id: str
    op_type: int = 1  # 1 置顶 / 0 取消''')

# ============================================================ server: 启动迁移
s("startup-migrate", '''    storage.migrate_posts_to_finder(_fid_map)
except Exception as _e:
    logger.warning(f"[启动] 作品归属迁移失败(不影响启动,下次再试): {_e}")''',
  '''    storage.migrate_posts_to_finder(_fid_map)
except Exception as _e:
    logger.warning(f"[启动] 作品归属迁移失败(不影响启动,下次再试): {_e}")
# rev24: 评论域归属也从「登录卡片 account_id」迁到「视频号 finder_id」(幂等)
try:
    _cm = storage.migrate_comments_to_finder(_fid_map)
    logger.info(f"[启动] 评论归属迁移: {_cm}")
except Exception as _e:
    logger.warning(f"[启动] 评论归属迁移失败(不影响启动,下次再试): {_e}")''')

# ============================================================ server: 归属工具函数
s("helpers", '''# ===================== 评论 =====================
def _own_identity_map():
    """每个账号「自己的身份」(视频号 id + 昵称),供「隐藏发出的评论」判定。两来源取并集:

    1) accounts 配置:登录抓到的 _log_finder_id / locked_finder_id 与账号名;
    2) 库内统计:该账号评论里出现最多且占比够高的作者 id / 昵称
       (账号删掉重建、account_id 变了时,旧 id 上的自己评论也能认出来)。
       只认「命中已知账号身份白名单」的统计结果,避免把刷屏客户误判成自己。
    """
    m = {}

    def add(acc_id, ids=(), names=()):
        if not acc_id:
            return
        e = m.setdefault(acc_id, {"ids": set(), "names": set()})
        e["ids"].update(x for x in ids if x)
        e["names"].update(x for x in names if x)

    allow_ids, allow_names = set(), set()
    for a in config.get("accounts", []):
        acc = a.get("id")
        ids = (a.get("_log_finder_id"), a.get("locked_finder_id"))
        names = (a.get("name"), a.get("locked_name"))
        allow_ids.update(x for x in ids if x)
        allow_names.update(x for x in names if x)
        add(acc, ids=ids, names=names)
    try:
        learned = storage.own_identities_from_library(allow_ids=allow_ids,
                                                      allow_names=allow_names)
        for acc, ident in learned.items():
            add(acc, ids=ident.get("ids") or (), names=ident.get("names") or ())
    except Exception:
        logger.exception("统计自己评论身份失败")
    return m''',
  '''# ===================== 评论(归属 = 视频号 finder_id,不是登录卡片) =====================
def _fid_of(account_id):
    """登录卡片 id -> 归属视频号。取不到用 acct:<卡片> 兜底(与 storage 迁移口径一致)。"""
    return _resolve_finder(account_id) or ("acct:" + (account_id or "unknown"))


def _comment_worker(finder_id=None, account_id=None):
    """评论写操作通道:先按视频号找当前登录着它的卡片,再退回前端指定的卡片。

    归属改按视频号后,一条评论的 account_id 可能指向早就删掉的卡片,
    只按 account_id 取 worker 会误报「账号未启动或未登录」。
    """
    if finder_id:
        for _aid, w in list(manager.workers.items()):
            a = getattr(w, "account", None) or {}
            cur = (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()
            if cur and cur == finder_id and w.logged_in:
                return w
    if account_id:
        w = manager.get_worker(account_id)
        if w and w.logged_in:
            return w
    return None


def _comment_finders():
    """视频号 -> {finder_id,name,account_id,account_ids,logged_in,comments,last_fetched}。

    配置里的视频号(可写操作)与库里出现过的视频号(含卡片已删的历史号)取并集 ——
    换卡片/删卡片之后,历史评论仍然按视频号看得到。
    """
    idx = _finder_index()
    out = {}
    for row in storage.comment_finders():
        fid = row.get("finder_id") or ""
        if not fid:
            continue
        e = idx.get(fid) or {}
        acc = e.get("account_id") or row.get("account_id") or ""
        out[fid] = {"finder_id": fid,
                    "name": e.get("name") or _finder_label(fid),
                    "account_id": acc,
                    "account_ids": e.get("account_ids") or ([acc] if acc else []),
                    "logged_in": bool(e.get("logged_in")),
                    "comments": row.get("comments") or 0,
                    "last_fetched": row.get("last_fetched")}
    for fid, e in idx.items():
        if fid not in out:
            out[fid] = {"finder_id": fid, "name": e.get("name") or _finder_label(fid),
                        "account_id": e.get("account_id") or "",
                        "account_ids": e.get("account_ids") or [],
                        "logged_in": bool(e.get("logged_in")), "comments": 0,
                        "last_fetched": None}
    return out


def _own_identity_map():
    """每个视频号「自己的身份」(视频号 id + 昵称),供「隐藏发出的评论」判定。两来源取并集:

    1) accounts 配置:登录抓到的 _log_finder_id / locked_finder_id 与账号名;
    2) 库内统计:该视频号评论里出现最多且占比够高的作者 id / 昵称
       (卡片删了重建、同视频号换卡片时,旧的自己评论也能认出来)。
       只认「命中已知身份白名单」的统计结果,避免把刷屏客户误判成自己。
    """
    m = {}

    def add(fid, ids=(), names=()):
        if not fid:
            return
        e = m.setdefault(fid, {"ids": set(), "names": set()})
        e["ids"].update(x for x in ids if x)
        e["names"].update(x for x in names if x)

    allow_ids, allow_names = set(), set()
    for a in config.get("accounts", []):
        fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()
        if not fid:
            continue
        names = (a.get("name"), a.get("locked_name"))
        allow_ids.add(fid)
        allow_names.update(x for x in names if x)
        add(fid, ids=(fid,), names=names)
    try:
        learned = storage.own_identities_from_library(allow_ids=allow_ids,
                                                      allow_names=allow_names)
        for fid, ident in learned.items():
            add(fid, ids=ident.get("ids") or (), names=ident.get("names") or ())
    except Exception:
        logger.exception("统计自己评论身份失败")
    return m''')

# ============================================================ server: 列表接口
s("list", '''@app.get("/api/comments")
async def get_comments(
    account_id: Optional[str] = None,
    replied: Optional[bool] = None,
    q: Optional[str] = None,
    hide_own: int = Query(0, ge=0, le=1),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    items, total = storage.query_comments(account_id=account_id, replied=replied, q=q,
                                          hide_own=bool(hide_own),
                                          own_map=_own_identity_map() if hide_own else None,
                                          limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}''',
  '''@app.get("/api/comments")
async def get_comments(
    finder_id: Optional[str] = None,
    account_id: Optional[str] = None,
    replied: Optional[bool] = None,
    q: Optional[str] = None,
    hide_own: int = Query(0, ge=0, le=1),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """评论列表。归属筛选用 finder_id(视频号);account_id 保留给旧前端与按卡片排查。"""
    items, total = storage.query_comments(finder_id=finder_id, account_id=account_id,
                                          replied=replied, q=q,
                                          hide_own=bool(hide_own),
                                          own_map=_own_identity_map() if hide_own else None,
                                          limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/comments/finders")
async def comment_finder_list():
    """评论归属视图:按视频号看评论量(含卡片已删的历史视频号)。"""
    items = sorted(_comment_finders().values(),
                   key=lambda x: (-(x.get("comments") or 0), x.get("name") or ""))
    return {"items": items}''')

# ============================================================ server: 批量删除
s("batch", '''    for item in body.items:
        w = manager.get_worker(item.account_id)
        if not w or not w.logged_in:
            failed.append({"comment_id": item.comment_id, "error": "账号未启动或未登录"})
            continue''',
  '''    for item in body.items:
        # 归属按视频号找登录卡片:评论行上的 account_id 可能已是被删掉的旧卡片
        fid = (getattr(item, "finder_id", None) or "").strip() or _fid_of(item.account_id)
        w = _comment_worker(fid, item.account_id)
        if not w or not w.logged_in:
            failed.append({"comment_id": item.comment_id,
                           "error": "该视频号没有已启动/已登录的账号卡片"})
            continue''')

s("batch-log", '''            # 删除前取评论信息,写入删除记录模块(手动删除)
            cinfo = storage.get_comment(item.comment_id)
            storage.delete_comment(item.comment_id)
            storage.log_delete(item.account_id, item.comment_id,
                               (cinfo or {}).get("nickname"), (cinfo or {}).get("content"),
                               "手动删除", item.export_id)''',
  '''            # 删除前取评论信息,写入删除记录模块(手动删除)
            cinfo = storage.get_comment(item.comment_id) or {}
            if cinfo.get("finder_id"):
                fid = cinfo["finder_id"]
            storage.delete_comment(item.comment_id)
            storage.log_delete(fid, item.comment_id,
                               cinfo.get("nickname"), cinfo.get("content"),
                               "手动删除", item.export_id, w.account["id"])''')

# ============================================================ server: 手动回复
s("reply", '''async def reply_comment(comment_id: str, body: schemas.ManualReply):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")''',
  '''async def reply_comment(comment_id: str, body: schemas.ManualReply):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")''')

s("reply-own", '''    _exp = (storage.get_comment(comment_id) or {}).get("export_id")
    if _ncid:
        storage.mark_own_comment(body.account_id, _ncid, "manual_reply",
                                 content=body.content, export_id=_exp)
    else:
        # 回复成功但接口没回 comment_id:记待确认,抓取时按 export_id+内容补登
        logger.warning(f"手动回复成功但未返回 commentId,已记待确认(抓到该评论后自动补登): {str(resp)[:200]}")
        storage.mark_own_comment_pending(body.account_id, _exp, body.content, "manual_reply")
    await hub.emit("comment_replied", {"comment_id": comment_id, "account_id": body.account_id})''',
  '''    _cinfo = storage.get_comment(comment_id) or {}
    _fid = _cinfo.get("finder_id") or fid          # 以评论行上的归属为准(更权威)
    _exp = _cinfo.get("export_id")
    _aid = w.account["id"]
    if _ncid:
        storage.mark_own_comment(_fid, _ncid, "manual_reply",
                                 content=body.content, export_id=_exp, account_id=_aid)
    else:
        # 回复成功但接口没回 comment_id:记待确认,抓取时按 export_id+内容补登
        logger.warning(f"手动回复成功但未返回 commentId,已记待确认(抓到该评论后自动补登): {str(resp)[:200]}")
        storage.mark_own_comment_pending(_fid, _exp, body.content, "manual_reply", _aid)
    await hub.emit("comment_replied", {"comment_id": comment_id, "finder_id": _fid,
                                       "account_id": _aid})''')

# ============================================================ server: 手动删除
s("delete", '''async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")''',
  '''async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")''')

s("delete-log", '''    # 删除前取评论信息,写入删除记录模块(手动删除)
    cinfo = storage.get_comment(comment_id)
    storage.delete_comment(comment_id)
    storage.log_delete(body.account_id, comment_id,
                       (cinfo or {}).get("nickname"), (cinfo or {}).get("content"),
                       "手动删除", body.export_id)''',
  '''    # 删除前取评论信息,写入删除记录模块(手动删除)
    cinfo = storage.get_comment(comment_id) or {}
    if cinfo.get("finder_id"):
        fid = cinfo["finder_id"]
    storage.delete_comment(comment_id)
    storage.log_delete(fid, comment_id,
                       cinfo.get("nickname"), cinfo.get("content"),
                       "手动删除", body.export_id, w.account["id"])''')

# ============================================================ server: 置顶
s("pin", '''async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")''',
  '''async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")''')

# ============================================================ server: 导出
s("export", '''@app.get("/api/comments/export")
async def export_comments(account_id: Optional[str] = None):
    def stream():
        import io
        buf = io.StringIO()
        wrt = csv.writer(buf)
        wrt.writerow(["账号", "视频ID", "评论ID", "用户", "内容", "时间", "点赞", "已回"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for r in storage.recent_comments(account_id=account_id, limit=10000):
            t = datetime.fromtimestamp(r[6]).strftime("%Y-%m-%d %H:%M:%S") if r[6] else ""
            wrt.writerow([r[0], r[1], r[2], r[3], r[4], t, r[7], "是" if r[8] else "否"])''',
  '''@app.get("/api/comments/export")
async def export_comments(finder_id: Optional[str] = None, account_id: Optional[str] = None):
    """导出评论 CSV(按视频号归属;不传则全量)。"""
    def stream():
        import io
        buf = io.StringIO()
        wrt = csv.writer(buf)
        wrt.writerow(["视频号", "视频ID", "评论ID", "用户", "内容", "时间", "点赞", "已回"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for r in storage.recent_comments(finder_id=finder_id, account_id=account_id, limit=10000):
            # 列序:finder_id,account_id,export_id,comment_id,nickname,content,head_url,
            #       create_time,like_count,replied
            t = datetime.fromtimestamp(r[7]).strftime("%Y-%m-%d %H:%M:%S") if r[7] else ""
            wrt.writerow([r[0], r[2], r[3], r[4], r[5], t, r[8], "是" if r[9] else "否"])''')

# ============================================================ server: 删除记录
s("del-logs", '''@app.get("/api/auto-delete/logs")
async def get_auto_delete_logs(
    account_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """自动删除记录(关键字命中删除日志)。带 account_name 便于前端展示。"""
    items, total = storage.query_delete_logs(account_id=account_id, q=q,
                                             limit=limit, offset=offset)
    name_map = {a["id"]: a.get("name", a["id"]) for a in config.get("accounts", [])}
    for it in items:
        it["account_name"] = name_map.get(it["account_id"], it["account_id"])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.delete("/api/auto-delete/logs")
async def clear_auto_delete_logs(account_id: Optional[str] = None):
    """清空删除记录(指定 account_id 则只清该账号)。"""
    storage.clear_delete_logs(account_id=account_id)
    return {"ok": True}''',
  '''@app.get("/api/auto-delete/logs")
async def get_auto_delete_logs(
    finder_id: Optional[str] = None,
    account_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """自动删除记录(关键字命中删除日志)。带 finder_name / account_name 便于前端展示。"""
    items, total = storage.query_delete_logs(finder_id=finder_id, account_id=account_id, q=q,
                                             limit=limit, offset=offset)
    name_map = {a["id"]: a.get("name", a["id"]) for a in config.get("accounts", [])}
    finders = _comment_finders()
    for it in items:
        it["account_name"] = name_map.get(it["account_id"], it["account_id"])
        f = finders.get(it.get("finder_id") or "") or {}
        it["finder_name"] = f.get("name") or _finder_label(it.get("finder_id"))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.delete("/api/auto-delete/logs")
async def clear_auto_delete_logs(finder_id: Optional[str] = None,
                                 account_id: Optional[str] = None):
    """清空删除记录(指定视频号则只清该视频号;account_id 为旧前端兼容)。"""
    storage.clear_delete_logs(finder_id=finder_id, account_id=account_id)
    return {"ok": True}''')

# ============================================================ apply
fail = []
for path, items in ((SCHEMAS, C_PATCHES), (SERVER, S_PATCHES)):
    s_ = io.open(path, encoding="utf-8").read()
    for tag, old, new in items:
        n = s_.count(old)
        if n != 1:
            fail.append(f"{path.split(chr(92))[-1]}:{tag} 命中 {n} 次")
            continue
        s_ = s_.replace(old, new, 1)
    if not fail:
        io.open(path, "w", encoding="utf-8", newline="").write(s_)
if fail:
    print("PATCH FAIL")
    for f in fail:
        print("  -", f)
    sys.exit(1)

# ============================================================ selfcheck
sv = io.open(SERVER, encoding="utf-8").read()
sc = io.open(SCHEMAS, encoding="utf-8").read()
checks = [
    ("migrate_comments_to_finder(_fid_map)" in sv, "启动迁移"),
    ("def _fid_of" in sv and "def _comment_worker" in sv and "def _comment_finders" in sv, "归属工具"),
    ('@app.get("/api/comments/finders")' in sv, "归属视图接口"),
    ("storage.query_comments(finder_id=finder_id" in sv, "列表按视频号"),
    ("w = _comment_worker(fid, body.account_id)" in sv and sv.count("_comment_worker(fid") == 3, "写操作通道"),
    ("storage.log_delete(fid, comment_id," in sv, "删除记录归属"),
    ("storage.mark_own_comment(_fid" in sv, "自己评论归属"),
    ("storage.recent_comments(finder_id=finder_id" in sv, "导出按视频号"),
    ('it["finder_name"]' in sv, "删除记录视频号名"),
    ("add(fid, ids=(fid,), names=names)" in sv, "身份映射按视频号"),
    ("finder_id: Optional[str] = None" in sc, "schema finder_id"),
]
bad = [t for ok, t in checks if not ok]
print("SELFCHECK", "PASS" if not bad else "FAIL: " + ", ".join(bad))
sys.exit(0 if not bad else 2)
