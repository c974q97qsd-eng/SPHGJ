"""FastAPI 服务:REST + WebSocket,托管 frontend/dist。

account_manager 跑在 uvicorn 同一事件循环(全异步,无需跨线程 run_coroutine_threadsafe)。
Hub 把后端事件(qr_update/login_status/comments_update/engine_status)广播给所有 WS 客户端。
"""
import os
import csv
import json
import sys
import gc
import types
import itertools
import asyncio
from collections import Counter
from datetime import datetime
from typing import Optional
import socket
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
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
from .memtrim import trim_now

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
    # 打印运行版本(懒导入 main.VERSION,避免与 main 的循环依赖)
    try:
        from main import VERSION
        logger.info(f"[启动] 视频号工具 v{VERSION} (FastAPI 已就绪)")
    except Exception:
        pass


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


@app.post("/api/system/memtrim")
async def manual_memtrim():
    """rev15 P0: 手动触发一次内存归还(gc.collect + 工作集压缩)。

    定时 trim 每 memtrim_interval_sec(默认 600s)执行一次,且首次也要等一个完整周期;
    提供手动端点便于立即验证效果 —— 直接对比返回里的 rss_mb_before / rss_mb_after,
    或与任务管理器中 Python 进程占用对照。
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(500, "未安装 psutil,无法读取进程内存")
    proc = psutil.Process()
    before = proc.memory_info().rss / 1024 / 1024
    # to_thread:gc.collect() 全量回收是同步阻塞,不能卡住 uvicorn 事件循环
    collected, ok = await asyncio.to_thread(trim_now)
    after = proc.memory_info().rss / 1024 / 1024
    logger.info(f"[memtrim] 手动触发: RSS {before:.1f}MB -> {after:.1f}MB"
                f"(省 {before - after:.1f}MB, gc 回收 {collected} 对象)")
    return {
        "ok": True,
        "gc_collected": collected,
        "working_set_trimmed": ok,
        "rss_mb_before": round(before, 1),
        "rss_mb_after": round(after, 1),
        "rss_mb_saved": round(before - after, 1),
    }


@app.get("/api/system/memdiag")
async def mem_diagnose(holders: bool = False):
    """rev15: 内存诊断 —— 报告进程 RSS,并按类型统计 gc 跟踪的存活对象。

    判读方法(关键):
      - tracked_mb 接近 rss_mb
          -> 内存主要在 Python 对象里,看 top_types 找具体类型,针对性优化;
      - tracked_mb 远小于 rss_mb(例:200MB vs 3500MB)
          -> 差值 untracked_mb 是【原生内存】:Playwright 的 C++ driver、
             Chromium 残留进程/共享内存、内存映射文件、未释放的原生 buffer。
             此时优化 Python 对象【无效】,应改查浏览器生命周期与原生资源释放。

    ?holders=1 追加"大容器定位"(rev17):找出 len >= 20000 的容器并瞥一眼内容,
      用于回答"这几百个小对象到底被谁持有" —— 数量与容器数不匹配时(如 849 万个 dict
      却只有 16 万个 list),大容器就是持有者。仅瞥前 3 个键/元素,绝不 repr 整个容器。
      另附 dict key 采样,用于判断 dict 的种类(CDP 事件 / 业务指标 / 评论 / 其它)。

    注意:这是重量级操作(遍历全部 gc 对象,大进程上需数秒且临时占用可观内存),
    仅用于排查,不要高频调用。
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(500, "未安装 psutil,无法读取进程内存")
    proc = psutil.Process()
    rss_mb = proc.memory_info().rss / 1024 / 1024
    holders_on = bool(holders)

    def _peek(o, n=3):
        """安全地瞥一眼大容器内容(绝不 repr 整个容器 —— 百万级容器会炸内存)。"""
        try:
            if isinstance(o, dict):
                ks = list(itertools.islice(o.keys(), n))
                return "keys: " + ", ".join(repr(k)[:38] for k in ks)
            if isinstance(o, (list, tuple, set, frozenset)):
                els = list(itertools.islice(o, n))
                return "els: " + ", ".join(type(e).__name__ for e in els)
        except Exception:
            pass
        return ""

    def _sample_dict_keys(objs, want=10, lo=3, hi=14):
        """采样小 dict 的 key 集合 —— 用于判断 dict 是 CDP 事件/业务指标/评论 还是别的。"""
        out = []
        for o in objs:
            if type(o) is not dict:
                continue
            try:
                n = len(o)
            except Exception:
                continue
            if lo <= n <= hi:
                try:
                    ks = sorted(str(k) for k in o.keys())[:12]
                except Exception:
                    continue
                if ks not in out:
                    out.append(ks)
                if len(out) >= want:
                    break
        return out

    def _referrer_chain(target, depth=3, min_len=20000):
        """自大容器向上追引用链,找出"谁持有它"。

        gc.get_referrers 是 O(全部gc对象) 的重操作,故只对最大的 1 个容器做、
        且限制深度。每层报告:持有者类型/长度/瞥一眼内容,并挑出其中最大的
        容器作为下一环 —— 这样能一路追到真正的根(模块级缓存/实例属性等)。
        """
        chain = []
        cur = target
        seen = {id(target)}
        for _ in range(depth):
            try:
                refs = gc.get_referrers(cur)
            except Exception:
                break
            best, best_len, kinds = None, -1, Counter()
            for r in refs:
                if r is cur or isinstance(r, (types.FrameType, types.TracebackType)):
                    continue
                tn = type(r).__name__
                try:
                    ln = len(r)
                except Exception:
                    ln = -1
                kinds[f"{tn}(len={ln})" if ln >= 0 else tn] += 1
                if ln > best_len and id(r) not in seen:
                    best, best_len = r, ln
            # len = 当前这一环自身的长度;held_by = 谁持有它(两者语义不同,不可混用)
            try:
                self_len = len(cur)
            except Exception:
                self_len = None
            chain.append({
                "type": type(cur).__name__,
                "len": self_len,
                "held_by": (f"{type(best).__name__}(len={best_len})" if best is not None else None),
                "referrer_kinds": [f"{k} x{v}" for k, v in kinds.most_common(6)],
                "peek": _peek(cur),
            })
            del refs
            if best is None or best_len < min_len:
                break
            seen.add(id(best))
            cur = best
        return chain

    def _collect():
        gc.collect()
        objs = gc.get_objects()
        counts = Counter()
        sizes = Counter()
        holders = []
        samples = []
        # 容器长度阈值:只关心"大到不正常"的容器(普通业务容器远达不到)
        HOLD_MIN = int(os.environ.get("MEMDIAG_HOLD_MIN", 20000))
        for o in objs:
            try:
                t = type(o).__name__
            except Exception:
                continue
            counts[t] += 1
            try:
                sizes[t] += sys.getsizeof(o)
            except Exception:
                pass
            if holders_on and t not in ("str", "bytes", "bytearray"):
                try:
                    ln = len(o)
                except Exception:
                    ln = -1
                if ln >= HOLD_MIN:
                    holders.append({
                        "type": t,
                        "len": ln,
                        "peek": _peek(o),
                        "__obj": o,   # 仅内部用于追链,序列化前剔除
                    })
        chain = []
        if holders_on:
            holders.sort(key=lambda x: -x["len"])
            holders = holders[:15]
            samples = _sample_dict_keys(objs)
            # 追链必须在 del objs 之后:objs 自身引用了所有对象,
            # 否则 get_referrers 会把这个巨大的临时 list 当成"持有者",污染结果。
            top_obj = holders[0]["__obj"] if holders else None
            del objs  # 尽早释放这份巨大的临时列表
            if top_obj is not None:
                chain = _referrer_chain(top_obj)
            top_obj = None
        else:
            del objs  # 尽早释放这份巨大的临时列表
        for h in holders:
            h.pop("__obj", None)
        return counts, sizes, holders, samples, chain

    # 重量级同步遍历放线程池,避免卡住 uvicorn 事件循环
    counts, sizes, holders, samples, chain = await asyncio.to_thread(_collect)
    top = [
        {"type": t, "count": counts[t], "size_mb": round(sizes[t] / 1024 / 1024, 2)}
        for t, _ in sizes.most_common(20)
    ]
    tracked_mb = sum(sizes.values()) / 1024 / 1024
    logger.info(f"[memdiag] RSS={rss_mb:.1f}MB 可跟踪对象={tracked_mb:.1f}MB "
                f"(原生/未跟踪 {rss_mb - tracked_mb:.1f}MB)")
    out = {
        "rss_mb": round(rss_mb, 1),
        "tracked_mb": round(tracked_mb, 1),
        "untracked_mb": round(rss_mb - tracked_mb, 1),
        "top_types": top,
    }
    if holders_on:
        out["big_holders"] = holders
        out["dict_key_samples"] = samples
        out["referrer_chain"] = chain  # 自最大容器向上追持有者
    return out


# ===================== 扫码登录(须在 {account_id} 路由之前注册,否则
# POST /api/accounts/login/start 会被 /api/accounts/{account_id}/start 抢匹配,
# account_id="login" -> 404 账号不存在)=====================

# 本机 IP 集合缓存(网卡 IP 极少变动,5 分钟 TTL 避免每次请求遍历网卡)
_LOCAL_IP_CACHE = {"ts": 0.0, "ips": set()}


def _local_ips():
    """本机所有 IP(回环 + 主机名解析 + 各网卡地址),用于判断客户端是否本机。"""
    now = time.time()
    if _LOCAL_IP_CACHE["ips"] and now - _LOCAL_IP_CACHE["ts"] < 300:
        return _LOCAL_IP_CACHE["ips"]
    ips = {"127.0.0.1", "::1", "localhost", ""}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ips.add(a.address)
    except Exception:
        pass
    _LOCAL_IP_CACHE.update(ts=now, ips=ips)
    return ips


def _client_is_local(request: Request) -> bool:
    """HTTP 客户端是否运行在本机。

    本机 -> headed 弹浏览器窗口扫码;远程(局域网其他主机) -> 服务器弹窗用户看不到,
    必须走 headless + 网页二维码推送。
    """
    host = (request.client.host if request.client else "") or ""
    host = host.replace("::ffff:", "")  # IPv4-mapped IPv6 归一化
    return host in _local_ips()


@app.post("/api/accounts/login/start")
async def login_start(request: Request, headed: Optional[bool] = Query(None)):
    # 未显式指定 -> 按客户端是否本机自动决定模式
    if headed is None:
        headed = _client_is_local(request)
    sess = await manager.start_login(headed=headed)
    return {"sid": sess.sid, "status": sess.status, "headed": headed}


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


@app.post("/api/accounts/login/{sid}/select-account")
async def login_select_account(sid: str, body: dict):
    """前端选择账号(选择页出现时推送列表给用户,用户选后回调此接口)。"""
    index = body.get("index", 0)
    ok = await manager.select_login(sid, index)
    if not ok:
        raise HTTPException(404, "登录会话不存在或选择失败")
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
async def relogin_account(account_id: str, request: Request,
                          headed: Optional[bool] = Query(None)):
    """已存在账号重新扫码登录(离线时前端启动失败转此)。

    headed 未指定时按客户端是否本机自动决定(远程走网页二维码)。
    """
    if headed is None:
        headed = _client_is_local(request)
    sess = await manager.start_relogin(account_id, headed=headed)
    if not sess:
        raise HTTPException(404, "账号不存在")
    return {"sid": sess.sid, "status": sess.status, "headed": headed}


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
            # 删除前取评论信息,写入删除记录模块(手动删除)
            cinfo = storage.get_comment(item.comment_id)
            storage.delete_comment(item.comment_id)
            storage.log_delete(item.account_id, item.comment_id,
                               (cinfo or {}).get("nickname"), (cinfo or {}).get("content"),
                               "手动删除", item.export_id)
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
    # 删除前取评论信息,写入删除记录模块(手动删除)
    cinfo = storage.get_comment(comment_id)
    storage.delete_comment(comment_id)
    storage.log_delete(body.account_id, comment_id,
                       (cinfo or {}).get("nickname"), (cinfo or {}).get("content"),
                       "手动删除", body.export_id)
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
