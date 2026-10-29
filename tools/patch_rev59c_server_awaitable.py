# -*- coding: utf-8 -*-
"""rev59c 补丁:_stop_everything 必须是「可等待的共享任务」,不能是「已开始就直接返回」。

单测发现的真问题(不是测试写错):
    graceful_shutdown 把清理排进循环 -> 清理开始(await stop_all)
    -> uvicorn 也走到 lifespan shutdown -> 发现 _shutdown["started"] 已 True
    -> 直接 return -> uvicorn 认为应用 shutdown 完毕 -> Runner 关闭事件循环
    -> _cancel_all_tasks 把【还在跑的清理任务】一起取消
结果:stop_all 被砍在中间,浏览器只关了一半 —— 和"完全没清理"一样糟,
而且日志上还写着「清理完成」,更容易误判。

改法:清理做成一个共享 task(_stop_task),所有调用方(线程触发的、lifespan 的)
都 await 同一个 task。lifespan shutdown 因此会【一直等到清理真的结束】才返回,
事件循环也就不会提前关闭。

用法: python tools/patch_rev59c_server_awaitable.py
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "server.py")

OLD = '''async def _stop_everything(path: str = "lifespan"):
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
'''

NEW = '''_stop_task = None          # 清理任务:全局共享,所有调用方都等它(见 _stop_everything)


async def _do_cleanup():
    """真正干活的那一次:停 manager + 登录会话 + playwright + 手动开的浏览器。"""
    t0 = time.time()
    logger.info(f"[shutdown] 开始清理({_shutdown['path']}):账号进程 "
                f"{len(getattr(manager, 'workers', None) or {})} 个")
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
'''

s = io.open(P, encoding="utf-8").read()
assert s.count(OLD) == 1, f"锚点命中 {s.count(OLD)} 次"
s = s.replace(OLD, NEW, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("ok   _stop_everything -> 共享 task + shield")

s2 = io.open(P, encoding="utf-8").read()
checks = [
    ("_stop_task 定义", "_stop_task = None" in s2),
    ("_do_cleanup 定义", "async def _do_cleanup():" in s2),
    ("create_task 共享", "_stop_task = asyncio.get_running_loop().create_task(_do_cleanup())" in s2),
    ("await shield", "await asyncio.shield(task)" in s2),
    ("取消时不谎报成功", '"cleanup cancelled"' in s2),
    ("旧的「已开始就直接返回」已消失", 'if _shutdown["started"]:\n        return' not in s2),
    ("lifespan 仍挂在 shutdown 事件", '@app.on_event("shutdown")' in s2),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("\nSELFCHECK", "PASS" if not bad else f"FAIL {bad}")
