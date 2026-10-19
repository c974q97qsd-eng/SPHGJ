# -*- coding: utf-8 -*-
"""rev25-3b: server 挂「自动发作品」接口 + 启动注册调度器。

接口一览(全部无鉴权,内网 LAN 信任,与既有接口口径一致):
  GET    /api/publish/status              调度器状态 + inbox + 未认领目录
  GET    /api/publish/tasks               队列/历史
  POST   /api/publish/scan                立即扫描 inbox
  POST   /api/publish/tasks/{id}/run      立即发这一条(绕过配额/时间窗)
  POST   /api/publish/tasks/{id}/skip     跳过
  POST   /api/publish/tasks/{id}/retry    重试(清熔断)
  POST   /api/publish/tasks/{id}/title    改标题
  DELETE /api/publish/tasks/{id}          删任务
  GET    /api/publish/titles              标题池
  POST   /api/publish/titles              批量加标题
  DELETE /api/publish/titles/{id}         删标题
  POST   /api/publish/titles/clear        清空池
  POST   /api/publish/settings            保存策略
  GET    /api/publish/finders             可选视频号(带标题余量/绑定卡片)
  POST   /api/publish/mappings            保存「目录 -> 视频号」绑定
"""
import io, sys

P = r"D:\SPHGJ\backend\server.py"
s = io.open(P, encoding="utf-8").read()
orig = len(s)

# ---------- 1) import ----------
OLD = "from .post_fetcher import PostFetcher"
NEW = "from .post_fetcher import PostFetcher\nfrom .publish_queue import PublishQueue"
assert s.count(OLD) == 1
s = s.replace(OLD, NEW, 1)

# ---------- 2) 实例化(放在 manager 之后) ----------
OLD2 = "manager = AccountManager(config, storage, emit=hub.emit)"
NEW2 = """manager = AccountManager(config, storage, emit=hub.emit)
# rev25: 自动发作品调度器(inbox 扫描 + 队列 + UI 发布)
publish_queue = PublishQueue(manager, storage, config, emit=hub.emit, save_config=save_config)
# 上次进程中断时还在 running 的任务不能自动重发(可能实际已发出),转 need_review 等人工核对
try:
    _nr = storage.reset_running_tasks()
    if _nr:
        logger.warning(f"[publish] {_nr} 条任务上次运行中断,已转「待核对」(不会自动重发)")
except Exception as _e:
    logger.warning(f"[publish] 收尾 running 任务失败(不影响启动): {_e}")"""
assert s.count(OLD2) == 1
s = s.replace(OLD2, NEW2, 1)

# ---------- 3) startup 注册 ----------
OLD3 = """    asyncio.create_task(_auto_restore_loop())"""
NEW3 = """    asyncio.create_task(_auto_restore_loop())
    # rev25: 自动发作品调度器(等自动恢复账号跑完再开始,见 PublishQueue._loop 的 20s 延迟)
    publish_queue.start()"""
assert s.count(OLD3) == 1
s = s.replace(OLD3, NEW3, 1)

# ---------- 4) 接口(插在作品归属视图之前) ----------
ANCHOR = '@app.get("/api/posts/finders")'
assert s.count(ANCHOR) == 1
APIS = '''# ==================== 自动发作品(rev25) ====================

@app.get("/api/publish/status")
async def publish_status():
    return publish_queue.status()


@app.get("/api/publish/finders")
async def publish_finders():
    """可发布视频号列表:带承载卡片、今日已发、标题余量、绑定状态。"""
    pool = storage.title_pool_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for acc in config.get("accounts") or []:
        fid = (acc.get("locked_finder_id") or acc.get("_log_finder_id") or "").strip()
        if not fid:
            continue
        w = manager.workers.get(acc.get("id"))
        logged = bool(w and w.logged_in) or bool((acc.get("_log_finder_id") or "") == fid)
        ps = pool.get(fid) or {}
        out.append({
            "finder_id": fid,
            "name": acc.get("name") or fid[-8:],
            "account_id": acc.get("id") or "",
            "running": bool(w),
            "logged_in": logged,
            "human_stopped": (acc.get("id") in getattr(manager, "human_stopped", set())),
            "today_success": storage.daily_success_count(fid, today),
            "titles_total": ps.get("total", 0),
            "titles_unused": ps.get("unused", 0),
            "last_success_at": storage.last_success_at(fid),
            "fail_streak": publish_queue._fail_streak.get(fid, 0),
        })
    generic = pool.get("") or {}
    return {"items": out, "generic_titles": {"total": generic.get("total", 0),
                                             "unused": generic.get("unused", 0)},
            "mappings": (config.get("publish") or {}).get("mappings") or {}}


@app.get("/api/publish/tasks")
async def publish_tasks(status: str = "", finder_id: str = "", limit: int = 100, offset: int = 0):
    rows, total = storage.query_tasks(status=status or None, finder_id=finder_id or None,
                                      limit=min(limit, 500), offset=max(0, offset))
    for r in rows:
        r["finder_name"] = publish_queue.finder_label(r.get("finder_id") or "")
    return {"items": rows, "total": total}


@app.post("/api/publish/scan")
async def publish_scan():
    stat = publish_queue.scan_inbox()
    filled = publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "scan": stat, "titles_filled": filled}


@app.post("/api/publish/tasks/{task_id}/run")
async def publish_run(task_id: int):
    if publish_queue._busy:
        raise HTTPException(409, "正在发布另一条,请等它完成(发布是全局串行的)")
    t = storage.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "success":
        raise HTTPException(400, "这条已经发布成功,不重复发(防重复)")
    # 手动发:清掉该视频号的熔断,重试要给人机会
    publish_queue._fail_streak[t.get("finder_id") or ""] = 0
    if t.get("status") in ("failed", "need_review", "skipped"):
        storage.reset_task(task_id)
    res = await publish_queue.run_task(task_id, manual=True)
    return {"ok": bool(res.get("ok")), "result": res}


@app.post("/api/publish/tasks/{task_id}/skip")
async def publish_skip(task_id: int):
    t = storage.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "running":
        raise HTTPException(400, "正在发布中,不能跳过")
    storage.set_task_status(task_id, status="skipped", step="skipped",
                            error="人工跳过", finished=True)
    if t.get("title_id"):
        storage.release_title(t["title_id"])
    await publish_queue._push()
    return {"ok": True}


@app.post("/api/publish/tasks/{task_id}/retry")
async def publish_retry(task_id: int):
    t = storage.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "running":
        raise HTTPException(400, "正在发布中")
    publish_queue._fail_streak[t.get("finder_id") or ""] = 0
    # 标题被还回池子后可能已被别的任务用掉 -> 重新取一条
    if not (t.get("title") or "").strip():
        tid, content = storage.take_title(t.get("finder_id") or "")
        if tid:
            storage.update_task_title(task_id, content)
            storage.bind_title_task(tid, task_id)
    storage.reset_task(task_id)
    await publish_queue._push()
    return {"ok": True}


@app.post("/api/publish/tasks/{task_id}/title")
async def publish_set_title(task_id: int, body: dict):
    t = storage.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    storage.update_task_title(task_id, title)
    return {"ok": True, "title": title}


@app.delete("/api/publish/tasks/{task_id}")
async def publish_delete(task_id: int):
    t = storage.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "running":
        raise HTTPException(400, "正在发布中,不能删除")
    storage.delete_task(task_id)
    return {"ok": True}


@app.get("/api/publish/titles")
async def publish_titles(finder_id: Optional[str] = None, include_used: bool = True):
    return {"items": storage.query_titles(finder_id=finder_id, include_used=include_used),
            "stats": storage.title_pool_stats()}


@app.post("/api/publish/titles")
async def publish_add_titles(body: dict):
    finder_id = (body.get("finder_id") or "").strip()
    raw = body.get("contents")
    if isinstance(raw, str):
        contents = raw.splitlines()
    elif isinstance(raw, list):
        contents = [str(x) for x in raw]
    else:
        raise HTTPException(400, "contents 需要是数组或换行文本")
    n = storage.add_titles(contents, finder_id=finder_id)
    filled = publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "added": n, "titles_filled": filled}


@app.delete("/api/publish/titles/{title_id}")
async def publish_delete_title(title_id: int):
    storage.delete_title(title_id)
    return {"ok": True}


@app.post("/api/publish/titles/clear")
async def publish_clear_titles(body: dict):
    finder_id = body.get("finder_id")
    only_used = bool(body.get("only_used"))
    n = storage.clear_titles(finder_id=finder_id, only_used=only_used)
    return {"ok": True, "deleted": n}


PUBLISH_SETTING_KEYS = ("enabled", "inbox_dir", "scan_interval_sec", "daily_limit",
                        "min_gap_sec", "gap_jitter_sec", "window_start", "window_end",
                        "auto_start_account", "min_login_remain_sec")


@app.post("/api/publish/settings")
async def publish_settings(body: dict):
    cfg = config.setdefault("publish", {})
    for k in PUBLISH_SETTING_KEYS:
        if k in body and body[k] is not None:
            cfg[k] = body[k]
    if isinstance(body.get("per_finder"), dict):
        cfg["per_finder"] = body["per_finder"]
    save_config(config)
    await publish_queue._push()
    return {"ok": True, "cfg": cfg}


@app.post("/api/publish/mappings")
async def publish_mappings(body: dict):
    """保存「inbox 目录名 -> 视频号」绑定。body: {"mappings": {"目录名": "finder_id"}}。"""
    mp = body.get("mappings")
    if not isinstance(mp, dict):
        raise HTTPException(400, "mappings 需要是 {目录名: finder_id} 对象")
    cfg = config.setdefault("publish", {})
    cur = cfg.get("mappings") or {}
    for k, v in mp.items():
        if v:
            cur[str(k)] = str(v)
        else:
            cur.pop(str(k), None)
    cfg["mappings"] = cur
    save_config(config)
    # 绑定后立刻重新扫一遍,把之前落在「未认领」里的文件收进来
    stat = publish_queue.scan_inbox()
    publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "mappings": cur, "scan": stat}


'''
s = s.replace(ANCHOR, APIS + ANCHOR, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = io.open(P, encoding="utf-8").read()
must = ["from .publish_queue import PublishQueue", "publish_queue = PublishQueue(",
        "publish_queue.start()", "/api/publish/status", "/api/publish/tasks",
        "/api/publish/titles", "/api/publish/settings", "/api/publish/mappings",
        "/api/publish/finders", "storage.reset_running_tasks()"]
miss = [m for m in must if m not in chk]
print(f"size {orig} -> {len(chk)}")
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
