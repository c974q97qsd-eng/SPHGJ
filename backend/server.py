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
import random
import threading
import urllib.request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("sphgj")

from .storage import Storage
from .account_manager import AccountManager
from .title_clean import split_titles
from .login_capture import LoginLockError
from .log_hub import install as install_log_hub
from . import schemas
from .metrics import metric_dictionary, validate_card_fields, DEFAULT_CARD_FIELDS
from .memtrim import trim_now
from .post_fetcher import PostFetcher
from .publish_queue import PublishQueue
from .publish_fixups import fix_mislabeled_need_review   # rev40: 历史误标回滚
from .remote_view import RemoteRegistry
from .browser import reap_profile_orphans
from . import platforms                          # rev56: 平台维度抽象（wx / dy）
from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）
from .douyin.login import DyLoginSession     # rev56j: 抖音扫码登录
from .douyin.worker import DouyinWorker      # rev56j: 抖音浏览器生命周期
from .local_window import local_windows, sweep_stale_view_profiles
from . import restart as restart_mod

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
# 日志中心:无控制台运行时(stdout/stderr 为 None 或不可见),日志走这里推到界面
log_hub = install_log_hub()
config = load_config()
storage = Storage(os.path.join(ROOT, config.get("db_path", "./data/comments.db")) or os.path.join(ROOT, "data/comments.db"))
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
# rev24: 评论域归属也从「登录卡片 account_id」迁到「视频号 finder_id」(幂等)
try:
    _cm = storage.migrate_comments_to_finder(_fid_map)
    logger.info(f"[启动] 评论归属迁移: {_cm}")
except Exception as _e:
    logger.warning(f"[启动] 评论归属迁移失败(不影响启动,下次再试): {_e}")
# rev27: 历史作品没有单独存播放地址,但 raw 里有 -> 启动补一次(幂等)
try:
    _nv = storage.backfill_video_urls()
    if _nv:
        logger.info(f"[启动] 回填作品播放地址 {_nv} 条")
except Exception as _e:
    logger.warning(f"[启动] 回填作品播放地址失败(不影响启动): {_e}")
manager = AccountManager(config, storage, emit=hub.emit)
# rev56: 抖音侧运行容器。它**不接管登录/浏览器启停** —— 抖音的扫码流程和
# 视频号完全不同，塞进 AccountWorker 会把已经稳定的在网状态机搞乱。
# 页面通过 set_page 注入（见 /api/dy/attach），没注入时所有网络方法都返回未就绪。
dy_manager = DouyinManager(config, storage, emit=hub.emit, save_config=save_config)
_dy_cdp = {}   # {pw, browser} —— 挂载的外部浏览器句柄，别让 GC 收掉
_dy_login_sessions = {}   # sid -> DyLoginSession
_dy_workers = {}          # account_id -> DouyinWorker
# rev25: 自动发作品调度器(inbox 扫描 + 队列 + UI 发布)
publish_queue = PublishQueue(manager, storage, config, emit=hub.emit, save_config=save_config)
# rev27: 远程同步浏览(CDP 串流),「打开」按钮走这里,5 分钟无操作自动断开
remote_registry = RemoteRegistry(manager, config, storage)
manager.remote_registry = remote_registry  # rev28: 清孤儿时避免误杀正在串流的浏览器
# 上次进程中断时还在 running 的任务不能自动重发(可能实际已发出),转 need_review 等人工核对
try:
    _nr = storage.reset_running_tasks()
    if _nr:
        logger.warning(f"[publish] {_nr} 条任务上次运行中断,已转「待核对」(不会自动重发)")
except Exception as _e:
    logger.warning(f"[publish] 收尾 running 任务失败(不影响启动): {_e}")

# rev40: 旧版 reset_running_tasks() 的 UPDATE 漏了 WHERE —— 只要启动时存在 1 条
# running，全表(含已 success / failed)都会被刷成 need_review。代码已修，
# 历史脏数据在这里一次性回滚：只回滚有「已发布」硬证据的，其余保留人工核对。
# 幂等、写审计(data/rev40_fix_audit.json)、异常不影响启动。
try:
    _fx = fix_mislabeled_need_review(storage.db_path, publish_queue.inbox_dir, logger)
    if _fx.get("recover") and not _fx.get("skipped"):
        logger.warning(f"[publish] rev40 历史误标修复: {_fx['recover']} 条 need_review 已确认为"
                       f"「实际已发布」并转回 success（仍保留待核对 {_fx.get('kept', 0)} 条）")
except Exception as _e:
    logger.warning(f"[publish] rev40 历史误标修复跳过(不影响启动): {_e}")

app = FastAPI(title="视频号评论区管理")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 捕获 uvicorn 事件循环,供主线程关闭时同步停 manager + playwright
_LOOP: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def _capture_loop():
    global _LOOP
    _LOOP = asyncio.get_event_loop()
    # 日志中心绑定事件循环,之后可从任意工作线程安全地推送到界面
    log_hub.attach(_LOOP, hub.emit)
    asyncio.create_task(_mem_monitor())
    # rev18: 泄漏高发期(启动后 ~3.5h)自动深采样,结果落盘 mem_deep.log
    asyncio.create_task(_deep_autopsy_loop())
    # 作品每日定时刷新(手动刷新不受影响)
    asyncio.create_task(_posts_auto_refresh_loop())
    # rev21: 重启后恢复"关闭之前在运行中的账号"
    # 延迟 15s:等前端 WS 连上先拿到真实状态,恢复过程经 engine_status 事件实时可见
    async def _auto_restore_loop():
        # rev32: 先把指向扫码临时目录的 profile 搬回正常路径。
        # 必须赶在自动恢复拉起账号之前(profile 空着才搬得动)。
        try:
            await manager.heal_login_profiles()
        except Exception as e:
            logger.warning(f"[自愈] 执行异常(忽略): {e}")
        await asyncio.sleep(15)
        if not manager.config.get("auto_restore_running", True):
            logger.info("[恢复] auto_restore_running=false,跳过自动恢复")
            return
        try:
            await manager.restore_running_accounts()
        except Exception as e:
            logger.warning(f"[恢复] 自动恢复异常: {e}")
    asyncio.create_task(_auto_restore_loop())
    # rev25: 自动发作品调度器(等自动恢复账号跑完再开始,见 PublishQueue._loop 的 20s 延迟)
    publish_queue.start()
    # rev56j: 自动拉起抖音账号（有 platform=dy 的账号才动；没有就完全无操作）
    async def _dy_auto_start():
        await asyncio.sleep(25)
        if config.get("dy_auto_start", True) is False:
            logger.info("[dy] dy_auto_start=false，跳过自动启动")
            return
        accs = [a for a in config.get("accounts", []) if platforms.is_douyin(a)]
        if not accs:
            return
        for acc in accs:
            try:
                w = _dy_worker(acc.get("id"))
                if w is None:
                    continue
                res = await w.start(playwright=await _dy_pw())
                logger.info("[dy:%s] 自动启动: %s" % (acc.get("id"), res))
            except Exception as e:
                logger.warning("[dy:%s] 自动启动异常: %r" % (acc.get("id"), e))
    asyncio.create_task(_dy_auto_start())
    # rev31: 清掉上次进程遗留的本机窗口临时 profile(_view_*)
    try:
        sweep_stale_view_profiles()
    except Exception as e:
        logger.debug(f"[启动] 清理遗留窗口 profile 失败: {e}")
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


# ---------------- rev59: 停机链路 ----------------
# 历史问题:cleanup 从工作线程用 run_coroutine_threadsafe 往 uvicorn 的循环里排,
# 每次重启都打
#     WARNING graceful_shutdown 异常: Event loop is closed
#     RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
# 结果【清理一次都没跑成】:浏览器没关、profile 锁没释放,main.py 直接 os._exit,
# Chromium 被硬砍(残留孤儿进程 + cookie 可能未落盘)。
#
# 现在把清理的【唯一所有者】交给 FastAPI 的 lifespan shutdown:它在事件循环内部、
# 循环关闭之前执行,任何退出路径(关窗口 / 远程重启 / Ctrl+C / uvicorn 自己退出)
# 都会走到。graceful_shutdown 只负责「触发 + 等」,所以可以重复调用(幂等)。
_shutdown = {
    "started": False,      # 清理已开始(含正在跑)
    "done": False,         # 清理已结束(含异常结束)
    "started_at": 0.0,
    "elapsed": 0.0,
    "path": "",            # 触发来源:lifespan / thread
    "error": "",
}
_shutdown_done = threading.Event()


def _loop_alive() -> bool:
    """uvicorn 的事件循环是否还可用。关闭后往里排协程只会拿到 RuntimeError。"""
    try:
        return _LOOP is not None and not _LOOP.is_closed()
    except Exception:
        return False


_stop_task = None          # 清理任务:全局共享,所有调用方都等它(见 _stop_everything)


async def _do_cleanup():
    """真正干活的那一次:停 manager + 登录会话 + playwright + 手动开的浏览器。"""
    t0 = time.time()
    # rev60: 以前只报 workers(常是 0 个),看不出「还在恢复过程中」这件事,
    # 于是「清理完成,账号进程 0 个」看起来像没问题,实际有账号正启动到一半被砍。
    logger.info(f"[shutdown] 开始清理({_shutdown['path']}):账号进程 "
                f"{len(getattr(manager, 'workers', None) or {})} 个,"
                f"启动中 {len(getattr(manager, '_starting', None) or {})} 个")
    try:
        await manager.stop_all()
    except asyncio.CancelledError:
        # 走到这里说明有人不等我们(强制退出),别谎报成功
        _shutdown["error"] = "cleanup cancelled"
        logger.warning("[shutdown] 清理被取消(进程被强制退出),浏览器可能残留")
        raise
    except Exception as e:
        _shutdown["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"[shutdown] 清理异常(继续退出): {e}")
    finally:
        _shutdown.update(done=True, elapsed=round(time.time() - t0, 2))
        _shutdown_done.set()
        try:
            left = len(asyncio.all_tasks()) - 1     # 减掉自己
        except Exception:
            left = -1
        logger.info(f"[shutdown] 清理完成,用时 {_shutdown['elapsed']}s,"
                    f"仍有 {left} 个后台任务(uvicorn 收尾时统一取消)")


async def _stop_everything(path: str = "lifespan"):
    """停后台一切;只跑一次,并发调用者【一起等同一个 task】。

    关键点(踩过):这里不能"已开始就直接 return"。uvicorn 的 lifespan shutdown
    会调本函数,如果它立刻返回,uvicorn 认为应用已经关完 -> Runner 关闭事件循环
    -> asyncio 把还在跑的清理任务一并取消 -> stop_all 被砍在中间,浏览器只关了
    一半,而日志还写着「清理完成」。改成共享 task 并 await,循环就必须等它跑完。
    """
    global _stop_task
    if _stop_task is None:
        _shutdown.update(started=True, started_at=time.time(), path=path)
        _stop_task = asyncio.get_running_loop().create_task(_do_cleanup())
    task = _stop_task
    try:
        await asyncio.shield(task)      # shield:调用方被取消也不影响清理本身
    except asyncio.CancelledError:
        if not _shutdown["done"]:
            raise
    except Exception:
        pass


@app.on_event("shutdown")
async def _on_lifespan_shutdown():
    """所有退出路径的清理入口(窗口关闭 / 远程重启 / Ctrl+C / 服务自己退出)。"""
    await _stop_everything("lifespan")


def graceful_shutdown(timeout: float = 20.0) -> bool:
    """确保后台清理完成,最多等 timeout 秒。返回 True = 已清理完成。

    幂等:清理由 lifespan shutdown 拥有,这里只「触发 + 等」。
    超时放宽到 20s:多账号各带直播 FLV 页,context.close 本来就慢。
    """
    if _shutdown_done.is_set():
        return True
    if not _shutdown["started"] and _loop_alive():
        # 先建协程再排;排失败必须 close(),否则 GC 时抛
        # "coroutine 'AccountManager.stop_all' was never awaited"。
        coro = _stop_everything("thread")
        try:
            asyncio.run_coroutine_threadsafe(coro, _LOOP)
        except Exception as e:
            coro.close()
            logger.warning(f"[shutdown] 无法把清理排进事件循环: {e}")
    elif not _loop_alive():
        # 循环已结束 = uvicorn 已经走到 lifespan shutdown,清理由它完成,这里只等
        logger.info("[shutdown] 事件循环已结束,清理交由 lifespan shutdown 完成")
    return _shutdown_done.wait(max(0.5, float(timeout)))


def wait_shutdown(timeout: float = 20.0) -> bool:
    """只等不触发(给 main.py 已经 poke 过 should_exit 之后用)。"""
    return _shutdown_done.wait(max(0.0, float(timeout)))


def shutdown_state() -> dict:
    """停机状态快照,挂到 /api/system/info 供远程排障。"""
    return {**_shutdown, "loop_alive": _loop_alive()}


# ===================== 配置 =====================
@app.get("/api/logs")
async def get_logs(after: int = 0):
    """拉取运行日志。after=0 取全部缓冲;否则只取 seq > after 的增量。"""
    lines = log_hub.history(after)
    return {"lines": lines, "seq": log_hub.seq}


@app.delete("/api/logs")
async def clear_logs():
    log_hub.clear()
    return {"ok": True}


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
        "ui_scale": config.get("ui_scale", "medium"),
        "posts": {
            "auto_refresh_enabled": bool((config.get("posts") or {}).get("auto_refresh_enabled", True)),
            "auto_refresh_hour": int((config.get("posts") or {}).get("auto_refresh_hour", 9)),
            "refresh_cooldown_sec": int((config.get("posts") or {}).get("refresh_cooldown_sec", 60)),
            "max_pages": int((config.get("posts") or {}).get("max_pages", 20)),
        },
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
    if body.ui_scale is not None:
        if body.ui_scale not in ("large", "medium", "small"):
            raise HTTPException(400, "ui_scale 仅支持 large/medium/small")
        config["ui_scale"] = body.ui_scale
    if body.posts_auto_refresh_enabled is not None:
        config.setdefault("posts", {})["auto_refresh_enabled"] = body.posts_auto_refresh_enabled
    if body.posts_auto_refresh_hour is not None:
        config.setdefault("posts", {})["auto_refresh_hour"] = body.posts_auto_refresh_hour
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



# ===================== rev18 内存深采样 =====================
# 背景: 取证发现泄漏为【2374 万个小 dict / 4.2GB, gc.collect 后仍存活】,
# 且无 len>=20000 的大容器 —— 被海量中型容器/闭包/实例属性分散持有。
# holders 模式(抓大容器)对此无效,只能随机抽样小 dict 统计"谁引用了它"。
DEEP_LOG = os.path.join(ROOT, ".workbuddy", "mem_deep.log")


def _deep_impl(sample_n=120, max_len=60):
    """随机小 dict 深采样: referrer 直方图 + key 分桶 + 数字 key 二层上溯。

    每个 gc.get_referrers 都是 O(全部 gc 对象) 的重操作,sample_n 不宜过大。
    """
    import psutil
    gc.collect()
    objs = gc.get_objects()
    pool = []
    for o in objs:
        if type(o) is dict:
            try:
                ln = len(o)
            except Exception:
                continue
            if 1 <= ln <= max_len:
                pool.append(o)
    n_small = len(pool)
    n = min(sample_n, n_small)
    sample = random.sample(pool, n) if n else []
    # 立刻释放全量池与 objs:否则它们会出现在每个样本的 referrers 里,污染直方图
    del objs, pool

    internal = {id(sample)}
    key_mix = Counter()    # 首 key 分桶: numeric-str / dunder / str / 其它类型
    has_numeric = 0        # 含纯数字字符串 key 的样本数(重点嫌疑: id() 注册表特征)
    len_hist = Counter()
    ref_kinds = Counter()      # 一层引用者直方图(全部样本)
    num_ref_kinds = Counter()  # 数字 key 样本的引用者直方图
    parent_kinds = Counter()   # 数字 key 中间容器的二层上溯
    peeks = []

    def _peek(o):
        try:
            if isinstance(o, dict):
                return "keys: " + ", ".join(repr(k)[:30] for k in itertools.islice(o.keys(), 3))
            if isinstance(o, (list, tuple, set)):
                return "els: " + ", ".join(type(e).__name__ for e in itertools.islice(o, 3))
        except Exception:
            pass
        return ""

    def _bucket(r):
        tn = type(r).__name__
        try:
            ln = len(r)
        except Exception:
            return tn
        if ln <= 8:
            return f"{tn}(len<=8)"
        if ln <= 64:
            return f"{tn}(len<=64)"
        if ln <= 512:
            return f"{tn}(len<=512)"
        return f"{tn}(len>512)"

    parents_done = 0
    for d in sample:
        try:
            ks = list(d.keys())
        except Exception:
            continue
        ln = len(ks)
        len_hist["1" if ln == 1 else ("2-8" if ln <= 8 else "9-60")] += 1
        num = any(isinstance(k, str) and k.isdigit() for k in ks)
        if num:
            has_numeric += 1
        k0 = ks[0]
        if isinstance(k0, str):
            key_mix["numeric-str" if k0.isdigit() else ("dunder" if k0.startswith("__") else "str")] += 1
        else:
            key_mix[type(k0).__name__] += 1
        try:
            refs = gc.get_referrers(d)
        except Exception:
            continue
        for r in refs:
            if type(r) in (types.FrameType, types.TracebackType) or id(r) in internal:
                continue
            rb = _bucket(r)
            ref_kinds[rb] += 1
            if num:
                num_ref_kinds[rb] += 1
                # 数字 key 的中间容器上溯一层找根(限次数,get_referrers 太重)
                if parents_done < 15 and isinstance(r, (dict, list, tuple)):
                    parents_done += 1
                    try:
                        refs2 = gc.get_referrers(r)
                    except Exception:
                        continue
                    for r2 in refs2:
                        if type(r2) in (types.FrameType, types.TracebackType) or id(r2) in internal:
                            continue
                        parent_kinds[_bucket(r2)] += 1
                        p = _peek(r2)
                        if p and len(peeks) < 12:
                            peeks.append(f"{type(r2).__name__}: {p}")
    return {
        "rss_mb": round(psutil.Process().memory_info().rss / 1048576, 1),
        "n_small_dicts": n_small,
        "sampled": len(sample),
        "has_numeric_key": has_numeric,
        "key_mix": dict(key_mix),
        "len_hist": dict(len_hist),
        "ref_kinds": dict(ref_kinds.most_common(20)),
        "numeric_key_ref_kinds": dict(num_ref_kinds.most_common(20)),
        "numeric_key_parent_kinds": dict(parent_kinds.most_common(20)),
        "peeks": peeks,
    }


def _chain_impl(max_lists=6, depth=6):
    """rev19: 对一个具体 list 逐层上溯引用链,定位海量小 dict 的真实持有者。

    与 _deep_impl 的区别(_deep_impl 到此为止,拿不到根):
      - _deep_impl 只统计"直接引用者类型"的直方图 —— 只能说"93% 被 list 持有",
        看不到 list 之上是谁,所以一直定位不到根因;
      - 本函数挑出"装满了小 dict 的 list"作为起点,逐层上溯到根
        (module / 实例属性 / frame),并且【保留 frame】—— 旧版把 FrameType 排除掉了,
        而"某个卡住的协程的局部变量"恰恰是最常见的持有者,排除它等于蒙上眼睛。
        frame 会被翻译成 函数名@文件:行号,直接指向代码位置。

    只读,不修改任何对象。随机性:取长度最大的若干 list(最具代表性)。
    """
    import psutil
    gc.collect()
    objs = gc.get_objects()

    # 1) 找"装满了 dict"的 list —— 泄漏批次的载体
    cands = []
    for o in objs:
        if type(o) is not list:
            continue
        n = len(o)
        if n < 8 or n > 4096:
            continue
        try:
            nd = sum(1 for e in o[:24] if type(e) is dict)
        except Exception:
            continue
        if nd >= max(4, int(min(24, n) * 0.6)):
            cands.append(o)
    if not cands:
        del objs, cands
        return {"error": "未找到 dict 批次 list(泄漏结构可能已变化)"}
    cands.sort(key=len, reverse=True)
    picks = cands[:max_lists]
    internal = {id(objs), id(cands), id(picks)}
    # 必须先释放全量对象表再追链:它引用了堆里的一切,会让每个对象的
    # referrers 里都出现这个巨大临时 list,彻底污染结果。
    del objs, cands

    def _peek(o, n=3):
        try:
            if isinstance(o, dict):
                return "keys: " + ", ".join(repr(k)[:26] for k in itertools.islice(o.keys(), n))
            if isinstance(o, (list, tuple, set, frozenset)):
                return "els: " + ", ".join(type(e).__name__ for e in itertools.islice(o, n))
        except Exception:
            pass
        return ""

    def _describe(o):
        d = {"type": type(o).__name__}
        try:
            d["len"] = len(o)
        except Exception:
            pass
        t = type(o)
        try:
            if t is types.FrameType:
                # 关键:报出代码位置,直接指向持有它的局部变量所在处
                fn = o.f_code.co_filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                d["frame"] = f"{o.f_code.co_name}@{fn}:{o.f_lineno}"
            elif t is types.ModuleType:
                d["module"] = getattr(o, "__name__", "?")
            elif hasattr(o, "__dict__") and not isinstance(o, (int, float, str, bytes, bool)):
                d["class"] = f"{t.__module__}.{t.__qualname__}"
        except Exception:
            pass
        p = _peek(o)
        if p:
            d["peek"] = p
        return d

    chains = []
    for c in picks:
        keys = []
        try:
            for e in c[:3]:
                if type(e) is dict:
                    keys.append(sorted(str(k) for k in itertools.islice(e.keys(), 8)))
        except Exception:
            pass
        node = {"list_len": len(c), "elem_key_samples": keys, "chain": []}
        cur, seen = c, {id(c)}
        for _ in range(depth):
            try:
                refs = gc.get_referrers(cur)
            except Exception:
                break
            pool = [r for r in refs
                    if id(r) not in seen and id(r) not in internal
                    and not isinstance(r, types.TracebackType)]
            if not pool:
                break

            def score(r):
                if isinstance(r, (list, dict, tuple, set)):
                    return 0
                if isinstance(r, types.FrameType):
                    return 1
                if isinstance(r, types.ModuleType):
                    return 2
                return 3
            pool.sort(key=score)
            top_score = score(pool[0])
            best = None
            for r in pool:
                if score(r) != top_score:
                    continue
                if best is None:
                    best = r
                    continue
                try:
                    if len(r) > len(best):
                        best = r
                except Exception:
                    pass
            if best is None:
                best = pool[0]
            node["chain"].append(_describe(best))
            seen.add(id(best))
            cur = best
            if isinstance(best, types.ModuleType):
                break
        chains.append(node)
    return {
        "rss_mb": round(psutil.Process().memory_info().rss / 1048576, 1),
        "mode": "chain",
        "chains": chains,
    }


def _tasks_impl():
    """rev21: asyncio 任务体检 —— 回答"这些 Task 是挂住了还是已完成却没人回收"。

    取证背景:2026-09-03 实测进程里有 7343 个 asyncio.Task 存活(每个 Task 自带一个
    contextvars.Context,故 Context/hamt 计数同步走高)。Task 挂起 = 协程帧常驻 =
    局部变量(含数百 KB 响应体文本)永久钉在堆上,而 str 不被 gc 跟踪,表现为
    untracked 内存。所以必须能区分:
      - done=False 且长时间不推进 -> 真挂起(CDP send / page.evaluate 未返回);
      - done=True 但仍被引用     -> 已完成却无人回收(持有者问题)。
    两种成因的修法完全不同,必须先分清。
    """
    try:
        import psutil as _ps
        rss_mb = round(_ps.Process().memory_info().rss / 1048576, 1)
    except Exception:
        rss_mb = -1
    try:
        all_t = asyncio.all_tasks()
    except RuntimeError:
        all_t = set()
    pending, done = [], []
    for t in all_t:
        try:
            coro = t.get_coro()
            code = getattr(coro, "cr_code", None)
            loc = f"{code.co_filename.rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1]}:{code.co_name}" if code else "?"
            # 挂起在哪一行:协程当前 yield 点(cr_frame 的 f_lineno)
            fr = getattr(coro, "cr_frame", None)
            if fr is not None:
                loc = f"{loc}@{fr.f_lineno}"
            rec = {"name": t.get_name(), "loc": loc}
            (done if t.done() else pending).append(rec)
        except Exception:
            continue
    def _top(records, n=12):
        c = Counter(r["loc"] for r in records)
        return [{"loc": k, "n": v} for k, v in c.most_common(n)]
    return {
        "rss_mb": rss_mb,
        "mode": "tasks",
        "total": len(all_t),
        "pending": len(pending),
        "done_unreclaimed": len(done),
        "pending_top": _top(pending),
        "done_top": _top(done),
        "verdict": (
            "大量 done=True 且长期不减 -> 已完成但被持有,查持有者"
            if len(done) > max(50, len(pending))
            else "大量 pending -> 真挂起,查 CDP/CDP send / page.evaluate 缺超时"
            if len(pending) > 50
            else "任务数正常"),
    }


async def _deep_autopsy_loop(runs=8, gap_sec=1500):
    """泄漏高发期自动深采样: 启动后每 25 分钟一次,共 8 次(~3.5h),之后自动停止。

    取证显示泄漏 dict 集中产生于启动后前 ~2.8h,故只在该窗口采样,无常驻开销。
    结果追加写 .workbuddy/mem_deep.log,供离线分析定位持有者。
    """
    os.makedirs(os.path.dirname(DEEP_LOG), exist_ok=True)
    for i in range(runs):
        await asyncio.sleep(gap_sec)
        # rev19: 先跑引用链上溯(能直接给出持有者/代码位置),再跑类型直方图。
        # chain 会逐层调用 gc.get_referrers,代价随堆增大而上升,故仅在堆还小时执行,
        # 避免在内存已经吃紧的大进程上雪上加霜。
        try:
            import psutil as _ps
            if _ps.Process().memory_info().rss / 1048576 < 3000:
                c = await asyncio.to_thread(_chain_impl)
                with open(DEEP_LOG, "a", encoding="utf-8") as f:
                    f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ")
                            + json.dumps(c, ensure_ascii=False) + "\n")
                logger.info(f"[memdeep] 引用链 #{i + 1}: 已写入 {len(c.get('chains', []))} 条链")
        except Exception as e:
            logger.debug(f"[memdeep] 引用链采样失败: {type(e).__name__}: {e}")
        try:
            d = await asyncio.to_thread(_deep_impl)
            with open(DEEP_LOG, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ")
                        + json.dumps(d, ensure_ascii=False) + "\n")
            logger.info(f"[memdeep] 深采样 #{i + 1}: RSS={d['rss_mb']}MB "
                        f"小dict={d['n_small_dicts']:,} 数字key样本={d['has_numeric_key']}")
        except Exception as e:
            logger.debug(f"[memdeep] 深采样失败: {type(e).__name__}: {e}")


@app.get("/api/system/comment-health")
async def comment_health():
    """评论抓取健康度自检 —— 回答「为什么好几天没抓到新评论」。

    2026-09-26 事故:早停判据(评论数没增加就跳过)会把视频永久锁死,
    表面看日志一直「扫描N视频 新增0评论」,但每个账号的指标都正常,
    肉眼查不出来。这里把关键指标直接暴露出来。

    判读:
      never_fetched > 0  且持续增长
          -> 有视频「接口说有评论、但我们一条都没抓到」,即早停被脏快照锁死。
             线上曾有个账号一次积了 231 个。
      last_scan 很久没更新
          -> 该账号根本没在跑(登录失效 / 被停用),不是抓取逻辑问题。

    修复:提交一个空的 POST 到 /api/system/comment-health/reset 复位脏快照,
    下一轮会自动补抓(也可用 ?reset=1 直接在这个接口里复位)。
    """
    def _impl():
        rows = storage.video_fetch_health()
        rows = sorted(rows, key=lambda r: -r["never_fetched"])
        return {"accounts": rows,
                "total_never_fetched": sum(r["never_fetched"] for r in rows)}
    return await asyncio.to_thread(_impl)


@app.post("/api/system/comment-health/reset")
async def comment_health_reset():
    """复位「有评论数快照但从未抓到评论」的脏视频,强制下一轮重抓。

    只动 video_stats 的计数,不碰 comments 表 —— 已抓到的评论一条不会丢。
    """
    def _impl():
        accs, n = storage.reset_stale_video_counts()
        logger.info(f"[comment-health] 复位脏快照: 影响 {accs} 个账号,清除 {n} 条")
        return {"accounts_affected": accs, "rows_reset": n}
    return await asyncio.to_thread(_impl)


_LOCAL_IPS = {"ips": None, "ts": 0.0}


def _local_ip_set():
    """本机所有 IPv4 地址(含回环)。缓存 60 秒,网卡热插拔也能跟上。"""
    now = time.time()
    if _LOCAL_IPS["ips"] is not None and now - _LOCAL_IPS["ts"] < 60:
        return _LOCAL_IPS["ips"]
    import socket
    ips = {"127.0.0.1", "::1"}
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ips.add(a.address)
    except Exception:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except Exception:
            pass
    _LOCAL_IPS["ips"], _LOCAL_IPS["ts"] = ips, now
    return ips


def _client_is_local(request):
    """请求是否来自「运行程序的这台机器」。

    判据:client.host in {127.0.0.1, ::1} 或命中本机任一网卡 IPv4。
    本机浏览器用局域网 IP(如 192.168.1.27)访问自己时,TCP 源地址就是本机网卡 IP,
    所以也能正确判为本机;别的电脑访问时源地址是它自己的 IP,判为非本机。
    """
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host in _local_ip_set()


@app.get("/api/system/whoami")
async def system_whoami(request: Request):
    """当前访问者是不是运行程序的这台机器(决定是否显示本机专属功能)。"""
    local = _client_is_local(request)
    return {"local": local,
            "client": (request.client.host if request.client else ""),
            "local_ips": sorted(_local_ip_set()) if local else []}


@app.get("/api/system/info")
async def system_info():
    """运行信息 —— 远程重启后靠 pid 变化判断是否重启成功。

    返回 pid / 启动时间 / 端口 / 版本 / 是否 headless,以及上一次重启的结果
    (restart.phase == "done" 且 ok == true 即重启完成)。
    该接口刻意保持极轻量:重启助手在等待新实例就绪时会高频轮询它。
    """
    return {"ok": True, **restart_mod.runtime_info(),
            "restart": restart_mod.read_restart_status(),
            "restart_wait": dict(_restart_wait),   # rev58: 是否在等发布任务结束
            "shutdown": shutdown_state()}          # rev59: 停机链路状态


@app.post("/api/publish/probe")
async def publish_probe(account_id: str = "", finder_id: str = "", upload_file: str = ""):
    """【诊断】dump 视频号发布页 DOM 结构(只读,不填写不提交)。

    发布页控件在 Shadow DOM 里、class 是编译产物,选择器必须按实测写。
    用某个已登录且运行中的账号打开 platform/post/create,把可交互元素
    (含 shadow root 层级 / iframe)落盘到 data/publish_dom_probe.json。
    upload_file: 传一个本机视频路径时会注入上传(仍不点发表),用来探测
    「上传后才渲染」的描述编辑器 —— 未上传时页面上根本没有描述框。
    """
    from . import publisher as pub
    w = manager.workers.get(account_id) if account_id else None
    if w is None and finder_id:
        for cand in manager.workers.values():
            if (cand.account.get("_log_finder_id") or "") == finder_id:
                w = cand
                break
    if w is None:
        raise HTTPException(400, "目标账号未运行,请先启动该账号再探测"
                                 "(或传 finder_id 找当前运行且登录该视频号的卡片)")
    if not w.logged_in:
        raise HTTPException(400, "目标账号未登录,请先扫码登录")
    ok = await w.ensure_browser()
    if not ok or w.context is None:
        raise HTTPException(500, "打开浏览器失败")
    try:
        out_dir = os.path.dirname(str(storage.db_path)) or "."
        res = await pub.probe_post_create(w.context, out_dir, upload_file=upload_file)
    finally:
        try:
            await w.release_idle_browser()
        except Exception:
            pass
    return {"ok": res.get("ok"), "account_id": w.account.get("id"),
            "file": res.get("file"), "final_url": res.get("final_url"),
            "logged_in": res.get("logged_in"),
            "has_file_input": res.get("has_file_input"),
            "upload_injected": res.get("upload_injected"),
            "upload_err": res.get("upload_err"),
            "upload_poll": res.get("upload_poll"),
            "publish_ready_at": res.get("publish_ready_at"),
            "desc_probe": res.get("desc_probe"),
            "key_nodes": res.get("key_nodes")}


def _url_playable(url, timeout=8):
    """CDN 地址是否还能拉(带 Range 只取 2 字节,便宜)。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except Exception:
        return False


@app.get("/api/posts/play")
async def post_play(finder_id: str = Query(""), object_id: str = Query("")):
    """取作品播放地址。存的地址过期时用该视频号的浏览器现取一次。

    CDN 直链带时效(实测 2025 年的地址已 400),所以不能只存一次就完事。
    """
    if not finder_id or not object_id:
        raise HTTPException(400, "缺少 finder_id / object_id")
    row = storage.get_post_video(finder_id, object_id)
    if row is None:
        raise HTTPException(404, "作品不存在")
    url = row.get("video_url") or ""
    refreshed = False
    if not await asyncio.to_thread(_url_playable, url):
        # 地址为空或已过期 -> 用登录着该视频号的 worker 现取
        aid = ""
        for a_id, w in (manager.workers or {}).items():
            if (w.account.get("_log_finder_id") or "").strip() == finder_id:
                aid = a_id
                break
        if not aid:
            acc = next((a for a in config.get("accounts", [])
                        if (a.get("_log_finder_id") or a.get("locked_finder_id") or "") == finder_id),
                       None)
            aid = (acc or {}).get("id") or ""
        w = (manager.workers or {}).get(aid)
        if w is not None and w.logged_in:
            acc_obj = w.account
            fresh = await PostFetcher(storage, acc_obj, config).find_video_url(w, object_id)
            if fresh:
                storage.update_post_video(finder_id, object_id, fresh)
                url, refreshed = fresh, True
    if not await asyncio.to_thread(_url_playable, url):
        return {"ok": False, "title": row.get("title") or "",
                "message": "这条作品的播放地址已失效,且当前无法现取"
                           "(视频号未运行/作品太旧不在列表前几页)。"
                           "启动该账号后重试,或到视频号后台查看。"}
    return {"ok": True, "url": url, "refreshed": refreshed,
            "title": row.get("title") or ""}


@app.websocket("/api/remote/ws")
async def remote_ws(ws: WebSocket, account: str = Query(""), url: str = Query("")):
    """远程同步浏览通道(rev27)。

    文本帧 = JSON 指令/元信息;二进制帧 = JPEG 画面。
    客户端 -> 服务端:{t:"mouse"|"text"|"key"|"nav"|"ping"|"close"}
    服务端 -> 客户端:{t:"opening"|"hello"|"url"|"closed"|"error"} + 二进制 JPEG

    会话期间该账号 worker 会让出 profile 锁(数据抓取暂停),断开/空闲超时后自动恢复。
    """
    await ws.accept()
    if not account:
        try:
            await ws.send_json({"t": "error", "message": "缺少 account 参数"})
            await ws.close()
        except Exception:
            pass
        return
    sess = None
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        sess, reused = await remote_registry.open(account, url or None)
        await ws.send_json(sess.meta())
    except Exception as e:
        logger.warning(f"[remote] 建立会话失败 {account}: {e}")
        try:
            await ws.send_json({"t": "error",
                                "message": f"{type(e).__name__}: {str(e)[:200]}"})
            await ws.close()
        except Exception:
            pass
        return

    async def reader():
        while True:
            msg = await ws.receive_json()
            t = msg.get("t")
            if t == "mouse":
                await sess.mouse(msg.get("kind") or "move", msg.get("x") or 0,
                                msg.get("y") or 0, msg.get("button") or "left",
                                dy=msg.get("dy") or 0, dx=msg.get("dx") or 0)
            elif t == "text":
                await sess.text(msg.get("text") or "")
            elif t == "key":
                await sess.key(msg.get("name") or "")
            elif t == "nav":
                await sess.nav(msg.get("action") or "", msg.get("url") or "")
            elif t == "close":
                return
            elif t == "ping":
                pass          # 只保活,不刷新「用户动作」时间(否则空闲超时永不触发)

    async def pusher():
        while not sess.closed:
            jpeg = await sess.next_frame(timeout=0.8)
            if jpeg:
                await ws.send_bytes(jpeg)
            if sess.closed:
                break

    async def urlwatch():
        last = ""
        while not sess.closed:
            await asyncio.sleep(1.2)
            cur = await sess.refresh_url()
            if cur and cur != last:
                last = cur
                await ws.send_json({"t": "url", "url": cur})

    tasks = [asyncio.create_task(reader()), asyncio.create_task(pusher()),
             asyncio.create_task(urlwatch())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
    except Exception as e:
        logger.debug(f"[remote] WS 循环异常: {e}")
    finally:
        reason = sess.close_reason or ("idle" if sess.closed else "client")
        try:
            await remote_registry.close(account, reason)
        except Exception:
            pass
        try:
            await ws.send_json({"t": "closed", "reason": reason})
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/remote/sessions")
async def remote_sessions():
    """当前串流会话(用于界面显示「谁在占用」)。"""
    return {"items": remote_registry.snapshot()}


@app.post("/api/remote/close")
async def remote_close(account: str = Query("")):
    """手动断开某账号的串流并恢复抓取。"""
    if not account:
        raise HTTPException(400, "缺少 account 参数")
    await remote_registry.close(account, "manual")
    return {"ok": True}


# ===================== 本机可见浏览器窗口(rev31) =====================
@app.post("/api/accounts/{account_id}/open-window")
async def account_open_window(account_id: str, request: Request):
    """在运行程序的本机拉起该账号的独立浏览器窗口。

    与后台抓取/直播大屏完全隔离:该账号 worker 活着时,从它的 context 只读导出
    登录态(storage_state),再用临时 profile 另起一个窗口;worker 没跑时 profile
    本来就空着,直接用。两种情形都不会占用后台那份 profile。
    窗口 5 分钟无操作自动关闭。
    """
    # 非本机访问不弹窗口:窗口会出现在运行程序的屏幕上,访问者根本看不到
    if not _client_is_local(request):
        raise HTTPException(403, "本机窗口只能在运行程序的这台机器上打开"
                                 "(远程请用「远程」按钮走网页串流)")
    acc = next((a for a in config.get("accounts", []) if a["id"] == account_id), None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    pw = await manager._ensure_playwright()
    r = await local_windows.open(manager, acc, pw)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "打开失败")
    await hub.emit("local_windows", {"items": local_windows.snapshot()})
    return r


@app.post("/api/accounts/{account_id}/close-window")
async def account_close_window(account_id: str):
    """关闭该账号的本机窗口(超时也会自动关)。"""
    r = await local_windows.close(account_id, "手动关闭")
    await hub.emit("local_windows", {"items": local_windows.snapshot()})
    if not r.get("ok"):
        raise HTTPException(404, r.get("error") or "没有打开的窗口")
    return r


@app.get("/api/local-windows")
async def local_window_list():
    """当前打开的本机窗口(用于界面显示)。"""
    return {"items": local_windows.snapshot()}


@app.post("/api/system/reap-orphans")
async def system_reap_orphans(account: str = Query("")):
    """清理锁死 profile 的孤儿 Chromium 进程(rev28)。

    不传 account 则遍历配置里的所有账号;正被占用(运行中 / 扫码中 / 串流中)
    的账号自动跳过,不会误杀。返回每个账号被 kill 的 pid 列表。
    """
    accs = [a for a in manager.config.get("accounts", [])
            if (not account or a.get("id") == account)]
    if account and not accs:
        raise HTTPException(404, "账号不存在")
    items = []
    for a in accs:
        aid = a.get("id") or ""
        if manager._profile_busy(aid):
            items.append({"account": aid, "skipped": "profile 正被占用(运行中/扫码中/串流中)"})
            continue
        pids = reap_profile_orphans(a.get("profile_dir"), log_tag=f"[reap:{aid}]")
        items.append({"account": aid, "profile_dir": a.get("profile_dir"),
                      "killed": pids})
    return {"items": items}


# rev58: 等待发布任务结束再重启的状态(前端据此提示,避免重启把正在发的作品打断)
_restart_wait = {"waiting": False, "since": 0.0, "max_wait": 0.0}


@app.post("/api/system/restart")
async def system_restart(delay: float = Query(3.0, ge=1, le=60),
                         wait_task: bool = False,
                         max_wait: float = Query(180, ge=0, le=1800)):
    """远程重启:优雅退出 + 按原方式重新拉起(见 backend/restart.py)。

    用法(内网任意机器,无需登录):
        curl -X POST "http://<运行机>:8712/api/system/restart?delay=3"
        curl -X POST "http://<运行机>:8712/api/system/restart?delay=3&wait_task=1"

    wait_task=1:若此刻有作品正在发布/上传,本接口【立刻返回】{"state":"waiting"},
    后台每 2 秒看一次,任务结束(或到 max_wait)后才真正退出。
    发布本来就是全局串行的、进度都落在库里,重启后能接着发,所以只等「当前这一条」,
    队列里其余任务不受影响。

    调用后【立刻返回】,真正退出发生在 delay 秒之后。客户端应轮询
    /api/system/info:pid 变了且服务可访问 = 重启成功;
    若长时间 pid 不变,看返回的 restart 字段里的 error。
    """
    if wait_task and publish_queue.busy_now():
        if _restart_wait["waiting"]:
            return {"ok": True, "state": "waiting", "already": True,
                    "waited_sec": round(time.time() - _restart_wait["since"], 1),
                    "max_wait": _restart_wait["max_wait"],
                    "hint": "已经在等当前这一条发完,不用重复点"}
        _restart_wait.update({"waiting": True, "since": time.time(),
                              "max_wait": max_wait})

        async def _restart_after_task():
            try:
                waited = await publish_queue.wait_idle(max_wait=max_wait, sleep=2.0)
                if publish_queue.busy_now():
                    logger.warning(f"[restart] 等了 {waited:.0f}s 发布仍未结束,"
                                   f"按 max_wait 强制重启;该任务重启后会被标成待核对")
                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                info = restart_mod.restart(delay)
                if info.get("already"):
                    logger.info(f"[restart] 已经安排过重启(helper_pid="
                                f"{info.get('helper_pid')}),本次不重复拉起")
            except Exception:
                logger.exception("[restart] 延迟重启失败")
            finally:
                _restart_wait["waiting"] = False

        asyncio.create_task(_restart_after_task())
        logger.info(f"[restart] 有作品正在发布,等它发完再重启(最多 {max_wait:.0f}s)")
        return {"ok": True, "state": "waiting", "waited_sec": 0.0,
                "max_wait": max_wait,
                "hint": f"有作品正在发布,发完才重启(最多等 {max_wait:.0f} 秒)"}
    try:
        info = restart_mod.restart(delay)
    except Exception as e:
        logger.exception("[restart] 启动重启失败")
        raise HTTPException(500, f"重启失败: {e}")
    if info.get("already"):
        # rev59: 同一进程 120s 内只允许安排一次重启(否则会拉起多个 helper,
        # 极端情况出现两个新实例抢同一批 profile)
        return {"ok": True, "state": "already", **info,
                "hint": f"{info.get('since', 0):.0f} 秒前已经安排过一次重启,"
                        f"本次不再重复拉起;继续等 pid 变化即可"}
    logger.info(f"[restart] 收到远程重启请求,{delay}s 后退出 "
                f"(helper_pid={info['helper_pid']}, wait_task={int(bool(wait_task))})")
    return {"ok": True, "state": "restarting", **info,
            "hint": "本进程将在 delay 秒后退出,请轮询 /api/system/info 确认新 pid"}


@app.get("/api/system/memdiag")
async def mem_diagnose(holders: bool = False, deep: bool = False, chain: bool = False,
                       tasks: bool = False):
    """rev15: 内存诊断 —— 报告进程 RSS,并按类型统计 gc 跟踪的存活对象。

    判读方法(关键):
      - tracked_mb 接近 rss_mb
          -> 内存主要在 Python 对象里,看 top_types 找具体类型,针对性优化;
      - tracked_mb 远小于 rss_mb(例:200MB vs 3500MB)
          -> 差值 untracked_mb 是【原生内存】:Playwright 的 C++ driver、
             Chromium 残留进程/共享内存、内存映射文件、未释放的原生 buffer。
             此时优化 Python 对象【无效】,应改查浏览器生命周期与原生资源释放。
    ?deep=1: rev18 随机小 dict 深采样(见 _deep_impl),定位海量小 dict 的持有者。

    ?holders=1 追加"大容器定位"(rev17):找出 len >= 20000 的容器并瞥一眼内容,
      用于回答"这几百个小对象到底被谁持有" —— 数量与容器数不匹配时(如 849 万个 dict
      却只有 16 万个 list),大容器就是持有者。仅瞥前 3 个键/元素,绝不 repr 整个容器。
      另附 dict key 采样,用于判断 dict 的种类(CDP 事件 / 业务指标 / 评论 / 其它)。

    注意:这是重量级操作(遍历全部 gc 对象,大进程上需数秒且临时占用可观内存),
    仅用于排查,不要高频调用。

    ?tasks=1: rev21 asyncio 任务体检(见 _tasks_impl),代价极小,可高频调用。
      用于区分「Task 真挂起」与「已完成但被持有」—— 两者修法完全不同:
      前者补超时,后者查持有者。Task 挂起时协程帧会钉住局部变量(含响应体文本),
      且 str 不被 gc 跟踪,表现为 untracked 内存上涨。
    """
    try:
        import psutil
    except ImportError:
        raise HTTPException(500, "未安装 psutil,无法读取进程内存")
    if deep:
        return await asyncio.to_thread(_deep_impl)
    if chain:
        return await asyncio.to_thread(_chain_impl)
    if tasks:
        # 只读当前存活 Task,代价极小,可放心高频调用
        return _tasks_impl()
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


def _login_popup_window() -> bool:
    """重新登录时是否弹本机浏览器窗口。

    rev40: 默认 False —— 不管本机还是远程,统一走「二维码推到前端网页」这套 UI。
    本机以前默认弹窗(能直接扫),但会抢焦点、盖住控制台,而且和远程是两套体验。
    要恢复弹窗:config.json 里加 "login_popup_window": true;
    单次想让本机弹窗:前端登录弹窗点「本机窗口」。
    """
    try:
        return bool(config.get("login_popup_window", False))
    except Exception:
        return False


@app.post("/api/accounts/login/start")
async def login_start(request: Request, headed: Optional[bool] = Query(None)):
    # 未显式指定 -> rev40 统一走网页二维码(默认不弹本机窗口)
    if headed is None:
        headed = _login_popup_window()
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


@app.post("/api/accounts/login/{sid}/retry-clean")
async def login_retry_clean(sid: str):
    """扫错微信(非锁定微信)确认后:重新进入全新登录环境等待扫码。

    登录态在检测到冲突时已清除,这里原地重启扫码流程(同一 sid)。
    """
    s = await manager.retry_login_clean(sid)
    if not s:
        raise HTTPException(404, "登录会话不存在或不在等待确认状态")
    return {"sid": s.sid, "status": s.status, "headed": s._headed}


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
    try:
        acc = await manager.finalize_login(sid, body.account_id, body.name)
    except LoginLockError as e:
        # 账号已锁定微信,本次登录的不是该微信 -> 不落盘,直接告知前端
        raise HTTPException(403, str(e))
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


@app.post("/api/accounts/{account_id}/lock")
async def lock_account(account_id: str):
    """锁定账号微信身份:之后该卡片只接受锁定的那个微信登录。"""
    if not manager.set_account_lock(account_id, True):
        raise HTTPException(400, "账号不存在或尚无微信标识(请先成功登录一次再锁定)")
    return {"ok": True, "locked": True, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/unlock")
async def unlock_account(account_id: str):
    """解锁:清除锁定的微信身份,恢复任意微信登录。"""
    if not manager.set_account_lock(account_id, False):
        raise HTTPException(404, "账号不存在")
    return {"ok": True, "locked": False, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/logout")
async def logout_account(account_id: str):
    """手动退出登录态(rev22):清该账号 profile 内的登录文件,账号配置保留。

    清理失败(浏览器没退干净)不抛 500,如实返回原因,前端据此提示重试 ——
    这是用户主动操作,失败原因必须可见,不能只留个红叉。
    """
    ok, msg = await manager.logout_account(account_id)
    if msg == "账号不存在":
        raise HTTPException(404, msg)
    return {"ok": ok, "message": msg, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/start")
async def start_account(account_id: str):
    w = await manager.start_account(account_id)
    if not w:
        # rev55: start_account 返回 None 有两种完全不同的情况,不能一律当 404:
        #   ① 配置里真的没有这个账号      -> 404
        #   ② 上次已判掉线且登录态没变过   -> 跳过启动(不白开浏览器),让前端转扫码
        # 原来混成 404,前端会显示「账号不存在」,把用户引到错误的方向。
        if not any(a.get("id") == account_id for a in config.get("accounts", [])):
            raise HTTPException(404, "账号不存在")
        logger.info(f"[{account_id}] 启动跳过:上次判定登录态已失效,请重新扫码")
        return {"ok": True, "logged_in": False, "need_scan": True,
                "auth_state": "offline",
                "message": "登录态已失效,需要重新扫码"}
    return {"ok": True, "logged_in": w.logged_in}


@app.post("/api/accounts/{account_id}/relogin")
async def relogin_account(account_id: str, request: Request,
                          headed: Optional[bool] = Query(None)):
    """已存在账号重新扫码登录(离线时前端启动失败转此)。

    headed 未指定时走网页二维码(rev40 默认不弹本机窗口)。
    """
    if headed is None:
        headed = _login_popup_window()
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


# ===================== 抖音：扫码登录 / 浏览器生命周期 =====================
async def _dy_emit(event, payload):
    try:
        await hub.emit(event, payload)
    except Exception as e:
        logger.debug("抖音事件推送失败 %s: %r" % (event, e))


async def _dy_pw():
    """复用 AccountManager 的 playwright 实例。

    同一个 playwright 进程能跑多个浏览器 context，没必要再起一套（省一份内存，
    也让「服务停止时一起收尾」只有一处逻辑）。
    """
    return await manager._ensure_playwright()


def _dy_accounts():
    return [a for a in config.get("accounts", []) if platforms.is_douyin(a)]


def _dy_account(account_id=None):
    accs = _dy_accounts()
    if account_id:
        return next((a for a in accs if a.get("id") == account_id), None)
    return accs[0] if accs else None


async def _dy_after_login(acc):
    """登录成功落盘后自动把该账号跑起来（开浏览器 + 起轮询）。

    放在后台任务里：扫码会话此刻正在收尾，别让开浏览器（几秒）卡住它。
    """
    aid = (acc or {}).get("id") or ""
    if not aid:
        return

    async def _run():
        try:
            w = _dy_worker(aid)
            if w is None:
                return
            res = await w.start(playwright=await _dy_pw(), force=True)
            logger.info("[dy:%s] 登录后自动启动: %s" % (aid, res))
        except Exception as e:
            logger.warning("[dy:%s] 登录后自动启动异常: %r" % (aid, e))

    asyncio.create_task(_run())


def _dy_worker(account_id=None):
    """取（或建）该账号的 worker。没有该账号返回 None。"""
    acc = _dy_account(account_id)
    if not acc:
        return None
    aid = acc.get("id") or ""
    w = _dy_workers.get(aid)
    if w is None:
        w = DouyinWorker(acc, config, storage, dy_manager, emit=_dy_emit)
        _dy_workers[aid] = w
    return w


@app.post("/api/dy/login/start")
async def dy_login_start(headed: Optional[bool] = Query(None),
                         account_id: Optional[str] = Query(None)):
    """开始抖音扫码登录。返回 sid，二维码通过 qr_update 事件推送（也可轮询本接口）。

    account_id 有值 = 给已有账号**重新登录**（沿用它的 id 与 profile 目录）。
    重新登录前必须先停掉该账号的 worker —— 否则 profile 被运行中的 context 独占，
    新 context 根本起不来（这也是视频号那边反复踩过的坑）。
    """
    if headed is None:
        headed = _login_popup_window()
    acc = None
    if account_id:
        acc = _dy_account(account_id)
        if not acc:
            raise HTTPException(404, "抖音账号不存在")
        w = _dy_workers.get(account_id)
        if w and w.status().get("running"):
            await w.stop()
            logger.info("[dy:%s] 重新登录前已停止运行中的 worker" % account_id)
    pw = await _dy_pw()
    sess = DyLoginSession(pw, config, _dy_emit, account=acc,
                          save_config=save_config, on_finalized=_dy_after_login)
    _dy_login_sessions[sess.sid] = sess
    await sess.start(headed=headed)
    return {"sid": sess.sid, "status": sess.status, "headed": headed,
            "platform": "dy", "account_id": sess.acc_id}


@app.get("/api/dy/login/{sid}")
async def dy_login_state(sid: str):
    """登录会话快照。image 字段给「不订阅 WS 也想拿二维码」的调用方用。"""
    s = _dy_login_sessions.get(sid)
    if not s:
        raise HTTPException(404, "抖音登录会话不存在")
    d = s.snapshot()
    if s.qr_image:
        d["image"] = s.qr_image
    if s.status in ("captured", "failed", "cancelled"):
        _dy_login_sessions.pop(sid, None)
    return d


@app.post("/api/dy/login/{sid}/cancel")
async def dy_login_cancel(sid: str):
    s = _dy_login_sessions.pop(sid, None)
    if not s:
        raise HTTPException(404, "抖音登录会话不存在")
    await s.cancel()
    return {"ok": True}


@app.get("/api/dy/workers")
async def dy_worker_list():
    return {"workers": [w.status() for w in _dy_workers.values()],
            "accounts": [platforms.account_brief(a) for a in _dy_accounts()]}


@app.post("/api/dy/accounts/{account_id}/start")
async def dy_account_start(account_id: str, force: bool = Query(False)):
    """启动抖音账号（开浏览器 + 硬验 + 起轮询）。

    未登录时返回 need_scan=True，前端据此走扫码流程 —— 这与视频号 rev55 的教训一致：
    「账号不存在」和「已判掉线跳过启动」是两种语义，混成 404 会误导前端。
    """
    acc = _dy_account(account_id)
    if not acc:
        raise HTTPException(404, "抖音账号不存在")
    w = _dy_worker(account_id)
    res = await w.start(playwright=await _dy_pw(), force=force)
    if not res.get("ok"):
        return {"ok": False, "need_scan": bool(res.get("need_scan")),
                "offline": bool(res.get("offline")),
                "msg": res.get("msg") or res.get("error") or "", "worker": w.status()}
    return {"ok": True, "worker": w.status()}


@app.post("/api/dy/accounts/{account_id}/stop")
async def dy_account_stop(account_id: str):
    w = _dy_workers.get(account_id)
    if not w:
        return {"ok": True, "msg": "该账号本来就未运行"}
    await w.stop()
    return {"ok": True, "worker": w.status()}


# ===================== 评论(归属 = 视频号 finder_id,不是登录卡片) =====================
def _fid_of(account_id):
    """登录卡片 id -> 归属视频号。取不到用 acct:<卡片> 兜底(与 storage 迁移口径一致)。"""
    return _resolve_finder(account_id) or ("acct:" + (account_id or "unknown"))


def _comment_platform(comment_id, finder_id=None):
    """这条评论属于哪个平台。**库里那一行最权威**（一条评论只属于一个作品、一个平台）。

    查不到这一行（比如前端拿的是过期 id）才用 finder_id 反查；
    再查不到按视频号处理 —— 保持改动前的老行为，不因为加了抖音就把老路径改坏。
    """
    row = storage.get_comment(comment_id) or {}
    p = row.get("platform")
    if p:
        return platforms.normalize_platform(p), row
    if finder_id:
        p = storage.platform_of(finder_id)
        if p:
            return platforms.normalize_platform(p), row
    return platforms.PLATFORM_WX, row


def _dy_ready():
    """抖音写通道就绪判定。没挂页面时必须明确报错，绝不能静默走视频号通道。"""
    st = dy_manager.status()
    if not st.get("configured"):
        raise HTTPException(400, "抖音账号未配置（config 里没有 platform=dy 的账号）")
    if not st.get("page_ready"):
        raise HTTPException(400, "抖音页面未就绪：需要先挂载已登录的浏览器（/api/dy/attach）")
    return st


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
    return m


@app.get("/api/comments")
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
    return {"items": items}


# ==================== rev56: 抖音（platform=dy）====================


@app.get("/api/dy/status")
async def dy_status():
    """抖音侧状态：有没有配账号 / 页面是否就绪 / 已抓评论数 / 上次抓取摘要。

    页面未就绪时 comments_total 等仍会返回 —— 数据是库里已有的，和浏览器无关。
    """
    return dy_manager.status()


@app.post("/api/dy/attach")
async def dy_attach(request: Request):
    """把一个**已登录** creator.douyin.com 的 Chromium 挂上来（CDP 调试端口）。

    为什么需要这个入口：抖音的扫码登录流程还没做（下一步的事），但抓取/回复的
    能力已经可用。先用「你自己开着调试端口登录好，再挂上来」的方式跑通全链路，
    比自己从零做一遍抖音扫码要快，也不会为了赶工把登录逻辑写歪。

    安全：必须 config.json 里 `"dy_dev_attach": true` 才允许，默认关。
    请求体：{"port": 9222}
    """
    if config.get("dy_dev_attach") is not True:
        raise HTTPException(403, "未开启 dy_dev_attach（在 config.json 里设 true 才允许挂载外部浏览器）")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        port = int((body or {}).get("port") or 9222)
    except (TypeError, ValueError):
        raise HTTPException(400, "port 必须是数字")
    if _dy_cdp.get("browser") is not None:
        raise HTTPException(409, "已经挂着一个浏览器了，先 /api/dy/detach")
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        raise HTTPException(500, f"当前环境没有 playwright，无法挂载: {e!r}")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as e:
        await pw.stop()
        raise HTTPException(502, f"连不上 127.0.0.1:{port}（需用 --remote-debugging-port={port} 启动）: {e!r}")
    page = None
    for c in browser.contexts:
        for p in c.pages:
            if "creator.douyin.com" in (p.url or ""):
                page = p
                break
        if page is not None:
            break
    if page is None:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://creator.douyin.com/creator-micro/content/manage")
        await page.wait_for_timeout(6000)
    _dy_cdp["pw"], _dy_cdp["browser"] = pw, browser
    dy_manager.set_page(page)
    ident = await dy_manager.refresh_identity()
    return {"ok": True, "url": page.url,
            "uid": ident.get("uid"), "sec_uid": ident.get("sec_uid"),
            "nickname": ident.get("nickname"),
            "status": dy_manager.status()}


@app.post("/api/dy/detach")
async def dy_detach():
    """断开挂载。只关我们这边的连接，**不关你的浏览器**（里面可能还开着别的业务）。"""
    dy_manager.set_page(None)
    br, pw = _dy_cdp.get("browser"), _dy_cdp.get("pw")
    _dy_cdp.clear()
    for obj in (br, pw):
        if obj is None:
            continue
        try:
            await obj.close() if obj is br else await obj.stop()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/dy/fetch")
async def dy_fetch(request: Request):
    """立刻抓一轮抖音评论。请求体可带 {"max_works": N} 限制本轮作品数（调试用）。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    mw = (body or {}).get("max_works")
    try:
        mw = int(mw) if mw else None
    except (TypeError, ValueError):
        mw = None
    rep = await dy_manager.fetch_now(max_works=mw)
    if not rep.get("__ok"):
        code = 503 if rep.get("__err") == "not_ready" else 502
        raise HTTPException(code, rep.get("__msg") or rep.get("__err"))
    # new_comments 可能上千条，接口只回摘要，明细走 /api/comments?finder_id=
    out = {k: v for k, v in rep.items() if k != "new_comments"}
    out["new_samples"] = (rep.get("new_comments") or [])[:20]
    return out


@app.post("/api/dy/comments/{comment_id}/reply")
async def dy_reply(comment_id: str, request: Request):
    """回复抖音评论。请求体 {"aweme_id": "...", "text": "..."}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    text = str((body or {}).get("text") or "").strip()
    aweme_id = str((body or {}).get("aweme_id") or "").strip()
    if not text:
        raise HTTPException(400, "回复内容不能为空")
    if not aweme_id:
        raise HTTPException(400, "缺少 aweme_id（评论列表里每条都带 export_id）")
    env = await dy_manager.reply(comment_id, aweme_id, text)
    if not env.get("__ok"):
        raise HTTPException(502, env.get("__msg") or env.get("__err") or "回复失败")
    return {"ok": True, "comment_id": comment_id}


@app.post("/api/dy/comments/{comment_id}/delete")
async def dy_delete_comment(comment_id: str, request: Request):
    """删除抖音评论。请求体 {"aweme_id": "..."}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    aweme_id = str((body or {}).get("aweme_id") or "").strip()
    if not aweme_id:
        raise HTTPException(400, "缺少 aweme_id")
    env = await dy_manager.delete_comment(comment_id, aweme_id)
    if not env.get("__ok"):
        raise HTTPException(502, env.get("__msg") or env.get("__err") or "删除失败")
    return {"ok": True, "comment_id": comment_id}


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
        # 归属按视频号找登录卡片:评论行上的 account_id 可能已是被删掉的旧卡片
        fid = (getattr(item, "finder_id", None) or "").strip() or _fid_of(item.account_id)
        # 抖音评论走抖音通道（同一条批量删除里可能混着两个平台的评论）
        plat, crow = _comment_platform(item.comment_id, fid)
        if plat == platforms.PLATFORM_DY:
            try:
                st = _dy_ready()
                aweme_id = str(item.export_id or (crow or {}).get("export_id") or "")
                env = await dy_manager.delete_comment(item.comment_id, aweme_id)
                if not env.get("__ok"):
                    failed.append({"comment_id": item.comment_id,
                                   "error": "删除失败: %s" % (env.get("__msg") or env.get("__err"))})
                    continue
                cinfo = crow or {}
                storage.delete_comment(item.comment_id)
                storage.log_delete(cinfo.get("finder_id") or fid, item.comment_id,
                                   cinfo.get("nickname"), cinfo.get("content"),
                                   "手动删除", aweme_id, st.get("account_id") or "",
                                   platform="dy")
                await hub.emit("comment_deleted", {"comment_id": item.comment_id,
                                                   "platform": "dy"})
                deleted += 1
            except HTTPException as e:
                failed.append({"comment_id": item.comment_id, "error": str(e.detail)})
            except Exception as e:
                failed.append({"comment_id": item.comment_id, "error": str(e)})
            continue
        w = _comment_worker(fid, item.account_id)
        if not w or not w.logged_in:
            failed.append({"comment_id": item.comment_id,
                           "error": "该视频号没有已启动/已登录的账号卡片"})
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
            cinfo = storage.get_comment(item.comment_id) or {}
            if cinfo.get("finder_id"):
                fid = cinfo["finder_id"]
            storage.delete_comment(item.comment_id)
            storage.log_delete(fid, item.comment_id,
                               cinfo.get("nickname"), cinfo.get("content"),
                               "手动删除", item.export_id, w.account["id"])
            await hub.emit("comment_deleted", {"comment_id": item.comment_id})
            deleted += 1
        except Exception as e:
            failed.append({"comment_id": item.comment_id, "error": str(e)})
    # 回收由各 worker 的延迟计时器统一处理(120s 无新操作才回收),无需在此逐一释放
    return {"ok": True, "deleted": deleted, "failed": failed}


@app.post("/api/comments/{comment_id}/reply")
async def reply_comment(comment_id: str, body: schemas.ManualReply):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        st = _dy_ready()
        aweme_id = str((crow or {}).get("export_id") or "")
        env = await dy_manager.reply(comment_id, aweme_id, body.content)
        if not env.get("__ok"):
            raise HTTPException(502, "回复失败: %s" % (env.get("__msg") or env.get("__err")))
        await hub.emit("comment_replied", {"comment_id": comment_id,
                                          "finder_id": (crow or {}).get("finder_id") or fid,
                                          "account_id": st.get("account_id") or "",
                                          "platform": "dy"})
        return {"ok": True, "platform": "dy"}
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")
    # 手动回复:保持浏览器,操作完成后延迟回收(连续回复只开一次进程)
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.reply_comment(comment_id, body.content)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"回复失败: {resp}")
    storage.mark_replied(comment_id)
    # 记录本工具发出的评论(「隐藏发出的评论」开关用)
    _data = resp.get("data") or {}
    _ncid = None
    if isinstance(_data, dict):
        _cmt = _data.get("comment") or {}
        if isinstance(_cmt, dict):
            _ncid = _cmt.get("commentId") or _cmt.get("comment_id")
        _ncid = _ncid or _data.get("commentId") or _data.get("comment_id")
    _cinfo = storage.get_comment(comment_id) or {}
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
                                       "account_id": _aid})
    return {"ok": True}


@app.delete("/api/comments/{comment_id}")
async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        st = _dy_ready()
        aweme_id = str(body.export_id or (crow or {}).get("export_id") or "")
        env = await dy_manager.delete_comment(comment_id, aweme_id)
        if not env.get("__ok"):
            raise HTTPException(502, "删除失败: %s" % (env.get("__msg") or env.get("__err")))
        cinfo = crow or {}
        _fid = cinfo.get("finder_id") or fid
        storage.delete_comment(comment_id)
        storage.log_delete(_fid, comment_id, cinfo.get("nickname"), cinfo.get("content"),
                           "手动删除", aweme_id, st.get("account_id") or "", platform="dy")
        await hub.emit("comment_deleted", {"comment_id": comment_id, "platform": "dy"})
        return {"ok": True, "platform": "dy"}
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")
    # 手动删除:保持浏览器,操作完成后延迟回收
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.delete_comment(body.export_id, comment_id)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"删除失败: {resp}")
    # 删除前取评论信息,写入删除记录模块(手动删除)
    cinfo = storage.get_comment(comment_id) or {}
    if cinfo.get("finder_id"):
        fid = cinfo["finder_id"]
    storage.delete_comment(comment_id)
    storage.log_delete(fid, comment_id,
                       cinfo.get("nickname"), cinfo.get("content"),
                       "手动删除", body.export_id, w.account["id"])
    await hub.emit("comment_deleted", {"comment_id": comment_id})
    return {"ok": True}


@app.post("/api/comments/{comment_id}/pin")
async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, _crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        # 抖音评论置顶接口还没探明（作品置顶是另一条 item/top）。宁可明确报错，
        # 也不能让它悄悄走视频号通道然后报「没有已登录的卡片」误导排查。
        raise HTTPException(400, "抖音评论置顶接口尚未探明，暂不支持")
    w = _comment_worker(fid, body.account_id)
    if not w or not w.logged_in:
        raise HTTPException(400, "该视频号没有已启动/已登录的账号卡片")
    # 手动置顶:保持浏览器,操作完成后延迟回收
    ok = await w.hold_for_manual()
    if not ok:
        raise HTTPException(400, "开浏览器失败,请检查登录状态")
    resp = await w.api.pin_comment(body.export_id, comment_id, body.op_type)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"置顶失败: {resp}")
    return {"ok": True}


@app.get("/api/comments/export")
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
            wrt.writerow([r[0], r[2], r[3], r[4], r[5], t, r[8], "是" if r[9] else "否"])
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
    return {"ok": True}


# ===================== 作品管理 =====================
def _parse_new_comment_id(resp):
    """从 create_comment 响应解析新评论 id。"""
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return None
    cmt = data.get("comment") or {}
    if isinstance(cmt, dict):
        cid = cmt.get("commentId") or cmt.get("comment_id")
        if cid:
            return cid
    return data.get("commentId") or data.get("comment_id")


# ===================== 作品归属:视频号(finder_id)= 主键 =====================
# 作品是「视频号」的资产,不是「登录卡片」的。卡片只提供登录态(抓取/写操作通道),
# 作品行里的 finder_id 才是归属;卡片删了/换号了,作品仍然属于那个视频号。
def _resolve_finder(account_id):
    """登录卡片 id -> 视频号 finder_id(以配置里当前的登录身份为准)。"""
    a = next((x for x in config.get("accounts", []) if x.get("id") == account_id), None)
    if not a:
        return ""
    return (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()


def _finder_label(fid):
    if not fid:
        return "未知视频号"
    if fid.startswith("acct:"):
        return "旧卡片 " + fid[5:]        # 卡片已删除、作品表也反查不到归属的历史数据
    return "视频号 " + fid[-8:]


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
    """作品刷新/批量操作后台任务(同一时间只允许一个,避免抢浏览器)。"""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.state = {"running": False, "kind": None, "accounts": {}, "total": 0,
                      "done": 0, "failed": [], "started_at": None, "finished_at": None}

    async def start(self, kind, coro):
        async with self.lock:
            if self.state["running"]:
                return False, f"已有任务在跑({self.state['kind']})"
            self.state = {"running": True, "kind": kind, "accounts": {}, "total": 0,
                          "done": 0, "failed": [], "started_at": datetime.now().isoformat(),
                          "finished_at": None}
        asyncio.create_task(self._run(kind, coro))
        return True, ""

    async def _run(self, kind, coro):
        try:
            await coro
        except Exception as e:
            logger.error(f"[posts:{kind}] 任务异常: {e}")
            self.state["failed"].append({"error": str(e)[:200]})
        finally:
            self.state["running"] = False
            self.state["finished_at"] = datetime.now().isoformat()


posts_job = PostsJob()
_posts_auto_done = {}


async def _do_posts_refresh(aids):
    st_all = posts_job.state
    st_all["accounts"] = {aid: {"status": "pending", "pages": 0, "fetched": 0,
                                "covers": 0, "error": ""} for aid in aids}
    st_all["total"] = len(aids)
    for aid in aids:
        st = st_all["accounts"][aid]
        st_all["done"] += 1
        w = manager.get_worker(aid)
        if not w:
            st["status"] = "not_found"; continue
        if not w.logged_in:
            st["status"] = "not_logged_in"; continue
        if not await w.ensure_browser():
            st["status"] = "browser_fail"; continue
        try:
            r = await PostFetcher(storage, w.account, config).refresh(w)
            st.update(r)
            if r.get("skipped"):
                st["status"] = "cooldown"
            elif r.get("error"):
                st["status"] = "error"
            else:
                st["status"] = "ok"
                # 账本按视频号记账:同一视频号换卡片登录不用重跑全量
                storage.set_post_fetch_meta(
                    (w.account.get("_log_finder_id") or "").strip() or aid,
                    account_id=aid, last_refresh=datetime.now().isoformat(),
                    last_pages=int(r.get("pages") or 0),
                    add_requests=int(r.get("pages") or 0) + int(r.get("covers") or 0),
                    full_synced=1 if r.get("full") else None)
            logger.info(f"[posts] {aid} 刷新完成: {st}")
        except Exception as e:
            st["status"] = "error"; st["error"] = str(e)[:200]
        finally:
            await w.release_idle_browser()
        await hub.emit("posts_refresh_progress", {"account_id": aid, **st})


async def _do_posts_batch(action, items):
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
        if not w:
            st["status"] = "not_logged_in"
            for oid in oids:
                st["failed"].append({"object_id": oid,
                                     "error": "该视频号当前没有已登录的账号卡片,请先启动/扫码登录"})
                st_all["done"] += 1
            continue
        try:
            for oid in oids:
                # hold 续期(浏览器保持),写操作经 api 客户端节流(4-8s/条) + 每日上限
                await w.hold_for_manual(delay=600)
                p = storage.get_post(fid, oid)
                if not p:
                    st["failed"].append({"object_id": oid, "error": "本地无记录,请先刷新"})
                    st_all["done"] += 1; continue
                skip = False
                if action == "hide" and p["visible_type"] == 3: skip = True
                if action == "unhide" and p["visible_type"] == 1: skip = True
                if action == "sticky" and p["sticky_op"] == 2: skip = True
                if action == "unsticky" and p["sticky_op"] != 2: skip = True
                if skip:
                    st["skipped"] += 1; st_all["done"] += 1; continue
                if action in ("hide", "unhide"):
                    resp = await w.api.update_post_visible(p["export_id"], VIS[action])
                    ok_call = bool(resp) and not resp.get("__err")
                    if ok_call:
                        storage.update_post_flags(fid, oid, visible_type=VIS[action])
                elif action == "sticky":
                    resp = await w.api.update_post_sticky(p["export_id"], 1)
                    ok_call = bool(resp) and not resp.get("__err")
                    if ok_call:
                        storage.update_post_flags(fid, oid, sticky_op=2)
                else:  # unsticky
                    resp = await w.api.update_post_sticky(p["export_id"], 2)
                    ok_call = bool(resp) and not resp.get("__err")
                    if ok_call:
                        storage.update_post_flags(fid, oid, sticky_op=0)
                if ok_call:
                    st["updated"] += 1
                else:
                    st["failed"].append({"object_id": oid,
                                         "error": str((resp or {}).get("__err") or resp)[:160]})
                st_all["done"] += 1
            st["status"] = "ok"
        except Exception as e:
            st["status"] = "error"; st["failed"].append({"error": str(e)[:200]})
        await hub.emit("posts_batch_progress", {"finder_id": fid, **st})


@app.get("/api/posts")
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


# ==================== 自动发作品(rev25) ====================

@app.get("/api/publish/status")
async def publish_status():
    return publish_queue.status()


@app.get("/api/publish/finders")
async def publish_finders():
    """可发布视频号列表:带承载卡片、今日已发、标题余量、绑定状态。"""
    pool = storage.title_pool_stats()
    counts = storage.task_counts_by_finder()      # rev58: 账号卡片上的条数
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
            # rev58: {"pending":n, "success":n, ...},前端按页签口径求和显示
            "counts": counts.get(fid) or {},
        })
    generic = pool.get("") or {}
    return {"items": out, "generic_titles": {"total": generic.get("total", 0),
                                             "unused": generic.get("unused", 0)},
            "mappings": (config.get("publish") or {}).get("mappings") or {}}


@app.get("/api/publish/tasks")
async def publish_tasks(status: str = "", finder_id: str = "", limit: int = 100,
                         offset: int = 0, order: str = ""):
    """查发布任务。order=queue/history 是【界面】排序(新的在前),
    不传则保持内部调度依赖的旧序(老任务先发),见 storage.query_tasks。"""
    rows, total = storage.query_tasks(status=status or None, finder_id=finder_id or None,
                                      limit=min(limit, 500), offset=max(0, offset),
                                      order=order or None)
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
async def publish_run(task_id: int, dry_run: bool = False):
    """立即发这一条。dry_run=1 只演练(上传+填描述,不点发表)。"""
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
    res = await publish_queue.run_task(task_id, manual=True, dry_run=dry_run)
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


@app.post("/api/publish/titles/preview")
async def publish_preview_titles(body: dict):
    """添加前预览:粘贴文本 -> 清理后的标题列表(不落库)。

    前端据此显示「识别到 N 条 / 已自动清理 / 跳过重复空行」,
    让用户加之前就知道序号、引号、全角空格那些格式会被去掉。
    """
    raw = body.get("contents")
    if not isinstance(raw, (str, list)):
        raise HTTPException(400, "contents 需要是数组或换行文本")
    contents, stat = split_titles(raw)
    return {"ok": True, "items": contents, "count": len(contents),
            "cleaned": stat["cleaned"], "dropped": stat["empty"] + stat["dup"]}


@app.post("/api/publish/titles")
async def publish_add_titles(body: dict):
    finder_id = (body.get("finder_id") or "").strip()
    raw = body.get("contents")
    if not isinstance(raw, (str, list)):
        raise HTTPException(400, "contents 需要是数组或换行文本")
    # rev41: 粘贴文本先清格式(行首序号 / 引号 / 全角空格 / 整段粘成一行),
    # 只清格式不动文案,话题和标点原样保留
    contents, stat = split_titles(raw)
    if not contents:
        raise HTTPException(400, "没识别到有效标题(粘贴的内容只有序号或空行?)")
    n = storage.add_titles(contents, finder_id=finder_id)
    filled = publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "added": n, "titles_filled": filled,
            "parsed": len(contents), "cleaned": stat["cleaned"],
            "dropped": stat["empty"] + stat["dup"]}


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


@app.get("/api/posts/job")
async def posts_job_status():
    """当前任务状态(刷新/批量进度轮询)。"""
    return posts_job.state


async def _posts_auto_refresh_loop():
    """每日定时:到达配置小时(默认 9 点)后,对已登录账号各刷一次(1h 内每小时整点重试直到成功)。"""
    while True:
        await asyncio.sleep(600)
        try:
            pc = config.get("posts") or {}
            if not pc.get("auto_refresh_enabled", True):
                continue
            if datetime.now().hour != int(pc.get("auto_refresh_hour", 9)):
                continue
            today = datetime.now().strftime("%Y-%m-%d")
            if _posts_auto_done.get("date") == today:
                continue
            aids = [aid for aid, w in manager.workers.items() if w.logged_in]
            if not aids:
                continue
            ok, _ = await posts_job.start("refresh", _do_posts_refresh(list(aids)))
            if ok:
                _posts_auto_done["date"] = today
                logger.info(f"[posts] 每日自动刷新已触发({len(aids)} 账号)")
        except Exception as e:
            logger.warning(f"[posts] 自动刷新循环异常: {e}")


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
# 作品封面(本地化落盘,前端永不直连 CDN)
_covers_dir = os.path.join(ROOT, "data", "covers")
os.makedirs(_covers_dir, exist_ok=True)
app.mount("/media/covers", StaticFiles(directory=_covers_dir), name="covers")

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
