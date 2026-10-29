# -*- coding: utf-8 -*-
"""rev59 补丁:server.py 停机链路 —— 清理的所有权从「工作线程排协程」改为
「FastAPI lifespan shutdown」+ 幂等 graceful_shutdown。

为什么要改(母本 run.log 实测):
    每次重启都会出现
        WARNING sphgj: graceful_shutdown 异常: Event loop is closed
        RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
    也就是【清理一次都没跑成】。main.py 的退出钩子随后 os._exit,
    Chromium 被硬砍 —— 残留孤儿进程要等下次启动 reap_profile_orphans 收拾,
    cookie 也可能来不及落盘(铁律:不能强杀 Chromium 还指望登录态还在)。

改法:
    - _stop_everything() 是唯一清理入口,内置「只跑一次」;无论成败都置完成标志。
    - 挂到 @app.on_event("shutdown") —— uvicorn 在【事件循环内部、循环关闭之前】
      执行它,任何退出路径(关窗口 / 远程重启 / Ctrl+C / uvicorn 自己退出)都会走到。
    - graceful_shutdown() 变成「触发 + 等」的幂等包装:
        * 循环还活着 -> 把清理排进去(排失败要把协程 close,否则 GC 报 never awaited)
        * 循环已结束 -> 说明 lifespan shutdown 早就跑过/正在跑,只等即可
    - 顺手把 shutdown 状态挂到 /api/system/info,排障时能看到是否卡在清理。

用法: python tools/patch_rev59_server.py
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "server.py")

OLD_IMPORT = """import time
import random
import urllib.request
"""
NEW_IMPORT = """import time
import random
import threading
import urllib.request
"""

OLD_FUNC = '''def graceful_shutdown(timeout: float = 15.0):
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
'''

NEW_FUNC = '''# ---------------- rev59: 停机链路 ----------------
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


async def _stop_everything(path: str = "lifespan"):
    """停 manager(workers + 登录会话 + playwright + 手动打开的浏览器)。

    只跑一次;无论成败都置完成标志 —— 否则调用方会一直等到超时。
    异常不能上抛:lifespan shutdown 里抛出去会让 uvicorn 记一条无关的退出错误。
    """
    if _shutdown["started"]:
        return
    _shutdown.update(started=True, started_at=time.time(), path=path)
    t0 = time.time()
    logger.info(f"[shutdown] 开始清理({path}):账号进程 "
                f"{len(getattr(manager, 'workers', None) or {})} 个")
    try:
        await manager.stop_all()
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
'''

OLD_INFO = '''    return {"ok": True, **restart_mod.runtime_info(),
            "restart": restart_mod.read_restart_status(),
            "restart_wait": dict(_restart_wait)}   # rev58: 是否在等发布任务结束
'''
NEW_INFO = '''    return {"ok": True, **restart_mod.runtime_info(),
            "restart": restart_mod.read_restart_status(),
            "restart_wait": dict(_restart_wait),   # rev58: 是否在等发布任务结束
            "shutdown": shutdown_state()}          # rev59: 停机链路状态
'''

PATCHES = [
    (OLD_IMPORT, NEW_IMPORT, "import threading"),
    (OLD_FUNC, NEW_FUNC, "graceful_shutdown -> 幂等 + lifespan shutdown"),
    (OLD_INFO, NEW_INFO, "/api/system/info 带 shutdown 状态"),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    n = s.count(old)
    assert n == 1, f"{tag}: 命中 {n} 次(期望 1)"
    s = s.replace(old, new, 1)
    print(f"ok  {tag}")

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---- 自校验 ----
s2 = io.open(P, encoding="utf-8").read()
checks = [
    ("import threading", "import threading" in s2),
    ("_stop_everything 定义", "async def _stop_everything(" in s2),
    ("lifespan shutdown 挂载", '@app.on_event("shutdown")' in s2),
    ("graceful_shutdown 幂等", "if _shutdown_done.is_set():" in s2),
    ("coro.close 兜底", "coro.close()" in s2),
    ("wait_shutdown", "def wait_shutdown(" in s2),
    ("shutdown_state", "def shutdown_state(" in s2),
    ("info 带 shutdown", '"shutdown": shutdown_state()' in s2),
    ("旧的一次性 run_coroutine_threadsafe(manager.stop_all()) 已消失",
     "run_coroutine_threadsafe(manager.stop_all()" not in s2),
    ("旧的 fut.result 已消失", "fut.result(timeout=timeout)" not in s2),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("\nSELFCHECK", "PASS" if not bad else f"FAIL {bad}")
