"""FastAPI 服务:REST + WebSocket,托管 frontend/dist。

account_manager 跑在 uvicorn 同一事件循环(全异步,无需跨线程 run_coroutine_threadsafe)。
Hub 把后端事件(qr_update/login_status/comments_update/engine_status)广播给所有 WS 客户端。
"""
import os
import csv
import json
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("sphgj")

from .storage import Storage
from .account_manager import AccountManager
from . import schemas
from .metrics import metric_dictionary, validate_card_fields, DEFAULT_CARD_FIELDS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
CONFIG_PATH = os.path.join(ROOT, "config.json")
FRONTEND_DIST = os.path.join(ROOT, "frontend", "dist")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        cfg = {"accounts": [], "fetch_interval_sec": 600,
               "auto_reply": {"enabled": False, "rules": []}, "db_path": "./data/comments.db",
               "risk_control": {"read_interval": [1.0, 2.5], "write_interval": [4.0, 8.0],
                                 "night_hours": [0, 6], "night_interval_multiplier": 3,
                                 "daily_write_limit": 100}}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class Hub:
    """WS 广播:后端事件 -> 所有连接前端。"""
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def emit(self, event, payload):
        if not self.clients:
            return
        msg = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()
config = load_config()
storage = Storage(os.path.join(ROOT, config.get("db_path", "./data/comments.db")) or os.path.join(ROOT, "data/comments.db"))
manager = AccountManager(config, storage, emit=hub.emit)

app = FastAPI(title="视频号评论区管理")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 捕获 uvicorn 事件循环,供主线程关闭时同步停 manager + playwright
_LOOP: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def _capture_loop():
    global _LOOP
    _LOOP = asyncio.get_event_loop()
    asyncio.create_task(_mem_monitor())


def _process_rss_mb() -> float:
    """当前进程 RSS(MB),Windows ctypes psapi(无第三方依赖);失败返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        if ok:
            return pmc.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return 0.0


async def _mem_monitor():
    """每 30 分钟记录主进程 RSS,用于监控内存增长曲线(验证精简效果)。"""
    while True:
        await asyncio.sleep(1800)
        mb = _process_rss_mb()
        if mb > 0:
            n_live = sum(1 for w in manager.workers.values() if w.live_fetcher is not None)
            n_total = len(manager.workers)
            logger.info(f"[mem] 主进程 RSS={mb:.0f}MB (账号={n_total}, 直播中={n_live})")


def graceful_shutdown(timeout: float = 15.0):
    """主线程(pywebview 窗口关闭后)调用:停 manager + playwright。

    超时放宽到 15s:多账号各自带直播 FLV 页,context.close 需要时间,
    原 5s 易超时;超时/异常时取消后台协程,避免进程退出时它仍往已断管道
    写事件触发驱动 EPIPE 崩溃。
    """
    if _LOOP is None:
        return
    fut = None
    try:
        fut = asyncio.run_coroutine_threadsafe(manager.stop_all(), _LOOP)
        fut.result(timeout=timeout)
    except Exception as e:
        logger.warning(f"graceful_shutdown 异常: {e}")
        if fut is not None:
            try:
                fut.cancel()
            except Exception:
                pass


# ===================== 配置 =====================
@app.get("/api/config")
async def get_config():
    return {
        "fetch_interval_sec": config.get("fetch_interval_sec", 300),
        "auto_reply": config.get("auto_reply", {"enabled": False, "rules": []}),
        "accounts_count": len(config.get("accounts", [])),
        "card_fields": config.get("card_fields") or DEFAULT_CARD_FIELDS,
        "dashboard_interval_sec": config.get("dashboard_interval_sec", 60),
        "live_check_interval_sec": config.get("live_check_interval_sec", 8),
        "manual_release_delay_sec": config.get("manual_release_delay_sec", 120),
    }


@app.patch("/api/config")
async def patch_config(body: schemas.ConfigUpdate):
    if body.fetch_interval_sec is not None:
        config["fetch_interval_sec"] = body.fetch_interval_sec
        # 热更新到运行中 worker
        for w in manager.workers.values():
            w.config = config
    if body.auto_reply_enabled is not None:
        config.setdefault("auto_reply", {})["enabled"] = body.auto_reply_enabled
        for w in manager.workers.values():
            w.auto_reply.auto_config = config["auto_reply"]
    if body.card_fields is not None:
        ok, err = validate_card_fields(body.card_fields)
        if not ok:
            raise HTTPException(400, err)
        config["card_fields"] = body.card_fields
    if body.dashboard_interval_sec is not None:
        config["dashboard_interval_sec"] = body.dashboard_interval_sec
        for w in manager.workers.values():
            w.config = config
    if body.live_check_interval_sec is not None:
        config["live_check_interval_sec"] = body.live_check_interval_sec
        for w in manager.workers.values():
            w.config = config
    if body.manual_release_delay_sec is not None:
        config["manual_release_delay_sec"] = body.manual_release_delay_sec
        for w in manager.workers.values():
            w.config = config
    save_config(config)
    return {"ok": True, "config": await get_config()}


# ===================== 扫码登录(须在 {account_id} 路由之前注册,否则
# POST /api/accounts/login/start 会被 /api/accounts/{account_id}/start 抢匹配,
# account_id="login" -> 404 账号不存在)=====================
@app.post("/api/accounts/login/start")
async def login_start():
    # 视频号拒绝在 headless 下渲染二维码(即使加 stealth),必须真实浏览器窗口
    sess = await manager.start_login(headed=True)
    return {"sid": sess.sid, "status": sess.status}


@app.post("/api/accounts/login/{sid}/open-window")
async def login_open_window(sid: str):
    s = manager.get_login_session(sid)
    if not s:
        raise HTTPException(404, "登录会话不存在")
    await s.open_window()
    return {"sid": sid, "status": s.status}


@app.post("/api/accounts/login/{sid}/cancel")
async def login_cancel(sid: str):
    await manager.cancel_login(sid)
    return {"ok": True}


@app.post("/api/accounts/login/{sid}/finalize")
async def login_finalize(sid: str, body: schemas.LoginFinalize):
    acc = await manager.finalize_login(sid, body.account_id, body.name)
    if not acc:
        raise HTTPException(404, "登录会话不存在或已完成")
    return {"ok": True, "account": acc, "accounts": manager.status_snapshot()["accounts"]}


# ===================== 账号 =====================
@app.get("/api/accounts")
async def get_accounts():
    return manager.status_snapshot()


@app.patch("/api/accounts/{account_id}")
async def patch_account(account_id: str, body: schemas.AccountUpdate):
    if body.name is not None and not manager.update_account(account_id, name=body.name):
        raise HTTPException(404, "账号不存在")
    if body.auto_comment_enabled is not None or body.auto_comment_content is not None:
        acc = next((a for a in config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            raise HTTPException(404, "账号不存在")
        enabled = body.auto_comment_enabled if body.auto_comment_enabled is not None else acc.get("auto_comment_enabled", False)
        content = body.auto_comment_content if body.auto_comment_content is not None else acc.get("auto_comment_content", "")
        manager.update_auto_comment(account_id, enabled, content)
    return {"ok": True, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/auto-comment")
async def set_auto_comment(account_id: str, body: schemas.AutoCommentConfig):
    if not manager.update_auto_comment(account_id, body.enabled, body.content):
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def del_account(account_id: str, remove_profile: bool = False):
    await manager.stop_account(account_id)
    if not manager.delete_account(account_id, remove_profile=remove_profile):
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/start")
async def start_account(account_id: str):
    w = await manager.start_account(account_id)
    if not w:
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "logged_in": w.logged_in}


@app.post("/api/accounts/{account_id}/relogin")
async def relogin_account(account_id: str):
    """已存在账号重新扫码登录(离线时前端启动失败转此)。"""
    sess = await manager.start_relogin(account_id)
    if not sess:
        raise HTTPException(404, "账号不存在")
    return {"sid": sess.sid, "status": sess.status}


@app.post("/api/accounts/{account_id}/stop")
async def stop_account(account_id: str):
    await manager.stop_account(account_id)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(account_id: str):
    """打开带登录态的 headed 浏览器供手动操作(停 worker,关浏览器后自动重启)。"""
    ok = await manager.open_account_browser(account_id)
    if not ok:
        raise HTTPException(404, "账号不存在或打开失败")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/open-dashboard")
async def open_dashboard(account_id: str):
    """打开当前直播的 dashboardV4 大屏(需账号在直播,停 worker,关浏览器后自动重启)。"""
    ok, msg = await manager.open_dashboard(account_id)
    if not ok:
        raise HTTPException(400, msg or "打开失败")
    return {"ok": True}


# ===================== 引擎 =====================
@app.post("/api/engine/start")
async def engine_start():
    await manager.start(headless=True)
    return manager.status_snapshot()


@app.post("/api/engine/stop")
async def engine_stop():
    await manager.stop()
    return manager.status_snapshot()


@app.post("/api/engine/fetch-now")
async def engine_fetch_now():
    if not manager.workers:
        raise HTTPException(400, "没有运行中的账号")
    asyncio.create_task(manager.fetch_all_once())
    return {"ok": True}


# ===================== 直播大屏 =====================
@app.get("/api/metrics/dictionary")
async def metrics_dictionary():
    """卡片可选指标清单(供前端配置 UI)。"""
    return {"metrics": metric_dictionary(), "card_fields": config.get("card_fields") or DEFAULT_CARD_FIELDS}


@app.get("/api/live-screen/status")
async def live_screen_status():
    """各账号直播快照(读 worker 缓存)。"""
    items = []
    for acc in config.get("accounts", []):
        w = manager.workers.get(acc["id"])
        info = getattr(w, "live_info", None) if w else None
        items.append({
            "account_id": acc["id"],
            "name": acc.get("name", acc["id"]),
            "logged_in": bool(w and w.logged_in),
            "live_stats": info.get("live_stats") if info else None,
            "stream_url": info.get("stream_url") if info else None,
            "updated_at": info.get("updated_at") if info else None,
            "is_live": bool(info.get("is_live")) if info else False,
            "metrics": info.get("metrics") if info else None,
        })
    return {"items": items, "card_fields": config.get("card_fields") or DEFAULT_CARD_FIELDS}


# ===================== 评论 =====================
@app.get("/api/comments")
async def get_comments(
    account_id: Optional[str] = None,
    replied: Optional[bool] = None,
    q: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    items, total = storage.query_comments(account_id=account_id, replied=replied, q=q,
                                          limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.post("/api/comments/batch-delete")
async def batch_delete_comments(body: schemas.BatchDeleteBody):
    """批量删除评论:按 account_id 取 worker 串行删,逐条广播 comment_deleted。

    手动操作经 hold_for_manual 保持浏览器:整批期间只开一次进程,完成后延迟回收
    (120s 无新操作才关),连续删除不反复开/关 Chromium。
    """
    deleted = 0
    failed: list[dict] = []
    held: dict = {}  # worker -> 本次 hold 是否成功(开浏览器失败则整批跳过该 worker)
    for item in body.items:
        w = manager.get_worker(item.account_id)
        if not w or not w.logged_in:
            failed.append({"comment_id": item.comment_id, "error": "账号未启动或未登录"})
            continue
        if w in held and not held[w]:
            failed.append({"comment_id": item.comment_id, "error": "开浏览器失败,请检查登录状态"})
            continue
        # 首次:开浏览器+计时;同一 worker 后续条目:仅续期(不重复开进程)
        ok = await w.hold_for_manual()
        held[w] = ok
        if not ok:
            failed.append({"comment_id": item.comment_id, "error": "开浏览器失败,请检查登录状态"})
            continue
        try:
            resp = await w.api.delete_comment(item.export_id, item.comment_id)
            if not resp or resp.get("__err"):
                failed.append({"comment_id": item.comment_id, "error": f"删除失败: {resp}"})
                continue
            storage.delete_comment(item.comment_id)
            await hub.emit("comment_deleted", {"comment_id": item.comment_id})
            deleted += 1
        except Exception as e:
            failed.append({"comment_id": item.comment_id, "error": str(e)})
    # 回收由各 worker 的延迟计时器统一处理(120s 无新操作才回收),无需在此逐一释放
    return {"ok": True, "deleted": deleted, "failed": failed}


@app.post("/api/comments/{comment_id}/reply")
async def reply_comment(comment_id: str, body: schemas.ManualReply):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")
    # 手动回复:保持浏览器,操作完成后延迟回收(连续回复只开一次进程)
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.reply_comment(comment_id, body.content)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"回复失败: {resp}")
    storage.mark_replied(comment_id)
    await hub.emit("comment_replied", {"comment_id": comment_id, "account_id": body.account_id})
    return {"ok": True}


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")
    # 手动删除:保持浏览器,操作完成后延迟回收
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.delete_comment(body.export_id, comment_id)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"删除失败: {resp}")
    storage.delete_comment(comment_id)
    await hub.emit("comment_deleted", {"comment_id": comment_id})
    return {"ok": True}


@app.post("/api/comments/{comment_id}/pin")
async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    w = manager.get_worker(body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "账号未启动或未登录")
    # 手动置顶:保持浏览器,操作完成后延迟回收
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.pin_comment(body.export_id, comment_id, body.op_type)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"置顶失败: {resp}")
    return {"ok": True}


@app.get("/api/comments/export")
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
            wrt.writerow([r[0], r[1], r[2], r[3], r[4], t, r[7], "是" if r[8] else "否"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
    headers = {"Content-Disposition": "attachment; filename=comments.csv"}
    return StreamingResponse(stream(), media_type="text/csv", headers=headers)


# ===================== 自动回复 =====================
@app.get("/api/auto-reply/rules")
async def get_rules():
    return config.get("auto_reply", {"enabled": False, "rules": []})


@app.patch("/api/auto-reply/rules")
async def set_rules(body: schemas.AutoReplyConfig):
    config["auto_reply"] = {"enabled": body.enabled,
                            "rules": [{"keyword": r.keyword, "reply": r.reply} for r in body.rules]}
    save_config(config)
    for w in manager.workers.values():
        w.auto_reply.auto_config = config["auto_reply"]
    return {"ok": True, "auto_reply": config["auto_reply"]}


# ===================== 自动删除 =====================
@app.get("/api/auto-delete/rules")
async def get_auto_delete():
    return config.get("auto_delete", {"enabled": False, "keywords": []})


@app.patch("/api/auto-delete/rules")
async def set_auto_delete(body: schemas.AutoDeleteConfig):
    config["auto_delete"] = {"enabled": body.enabled,
                             "keywords": [k.strip() for k in body.keywords if k.strip()]}
    save_config(config)
    for w in manager.workers.values():
        if w.auto_delete:
            w.auto_delete.update_config(config["auto_delete"])
    return {"ok": True, "auto_delete": config["auto_delete"]}


@app.get("/api/auto-delete/logs")
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
    return {"ok": True}


# ===================== WebSocket =====================
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    try:
        # 连接即推一次当前状态
        await ws.send_text(json.dumps({"event": "engine_status",
                                       "payload": manager.status_snapshot()}, ensure_ascii=False))
        while True:
            await ws.receive_text()  # 忽略客户端消息(心跳)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


# ===================== 前端静态托管 =====================
if os.path.isdir(FRONTEND_DIST):
    assets = os.path.join(FRONTEND_DIST, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api") or full_path.startswith("ws"):
            raise HTTPException(404)
        candidate = os.path.join(FRONTEND_DIST, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))
else:
    @app.get("/")
    async def index():
        return JSONResponse({"msg": "前端未构建,请在 frontend/ 下 npm run build",
                             "api": "/api/..."})
