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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("sphgj")

from .storage import Storage
from .account_manager import AccountManager
from .login_capture import LoginLockError
from .log_hub import install as install_log_hub
from . import schemas
from .metrics import metric_dictionary, validate_card_fields, DEFAULT_CARD_FIELDS
from .memtrim import trim_now
from .post_fetcher import PostFetcher

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
manager = AccountManager(config, storage, emit=hub.emit)

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
    return m


@app.get("/api/comments")
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
    # 记录本工具发出的评论(「隐藏发出的评论」开关用)
    _data = resp.get("data") or {}
    _ncid = None
    if isinstance(_data, dict):
        _cmt = _data.get("comment") or {}
        if isinstance(_cmt, dict):
            _ncid = _cmt.get("commentId") or _cmt.get("comment_id")
        _ncid = _ncid or _data.get("commentId") or _data.get("comment_id")
    if _ncid:
        storage.mark_own_comment(body.account_id, _ncid, "manual_reply")
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
                storage.set_post_fetch_meta(
                    aid, last_refresh=datetime.now().isoformat(),
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
        groups.setdefault(it.account_id, []).append(it.object_id)
    st_all["accounts"] = {aid: {"status": "pending", "updated": 0, "skipped": 0, "failed": []}
                          for aid in groups}
    st_all["total"] = len(items)
    VIS = {"hide": 3, "unhide": 1}
    for aid, oids in groups.items():
        st = st_all["accounts"][aid]
        w = manager.get_worker(aid)
        if not w or not w.logged_in:
            st["status"] = "not_logged_in"
            for oid in oids:
                st["failed"].append({"object_id": oid, "error": "账号未启动或未登录"})
                st_all["done"] += 1
            continue
        try:
            for oid in oids:
                # hold 续期(浏览器保持),写操作经 api 客户端节流(4-8s/条) + 每日上限
                await w.hold_for_manual(delay=600)
                p = storage.get_post(aid, oid)
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
                        storage.update_post_flags(aid, oid, visible_type=VIS[action])
                elif action == "sticky":
                    resp = await w.api.update_post_sticky(p["export_id"], 1)
                    ok_call = bool(resp) and not resp.get("__err")
                    if ok_call:
                        storage.update_post_flags(aid, oid, sticky_op=2)
                else:  # unsticky
                    resp = await w.api.update_post_sticky(p["export_id"], 2)
                    ok_call = bool(resp) and not resp.get("__err")
                    if ok_call:
                        storage.update_post_flags(aid, oid, sticky_op=0)
                if ok_call:
                    st["updated"] += 1
                else:
                    st["failed"].append({"object_id": oid,
                                         "error": str((resp or {}).get("__err") or resp)[:160]})
                st_all["done"] += 1
            st["status"] = "ok"
        except Exception as e:
            st["status"] = "error"; st["failed"].append({"error": str(e)[:200]})
        await hub.emit("posts_batch_progress", {"account_id": aid, **st})


@app.get("/api/posts")
async def get_posts(
    account_id: Optional[str] = None,
    q: Optional[str] = None,
    visible: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: str = "create_time",
    order: str = "desc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """作品查询(本地库,零官方请求)。visible: public/follow/hidden/sticky。"""
    items, total = storage.query_posts(
        account_id=account_id, q=q, visible=visible,
        date_from=date_from, date_to=date_to, sort=sort, order=order,
        limit=limit, offset=offset)
    name_map = {a["id"]: a.get("name", a["id"]) for a in config.get("accounts", [])}
    for it in items:
        it["account_name"] = name_map.get(it["account_id"], it["account_id"])
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/posts/meta")
async def get_posts_meta():
    """各账号抓取账本(上次刷新/今日请求次数)。"""
    out = {}
    for a in config.get("accounts", []):
        out[a["id"]] = storage.get_post_fetch_meta(a["id"])
    return {"meta": out}


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
    """批量操作:hide/unhide/sticky/unsticky(后台任务,写操作逐条节流)。"""
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
