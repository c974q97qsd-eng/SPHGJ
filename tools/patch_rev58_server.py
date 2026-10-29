"""rev58 一次性补丁:server.py(账号计数 / 任务排序透传 / 等任务发完再重启)。"""
import io
import os

P = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 os.pardir, "backend", "server.py"))


def apply(src, patches):
    for tag, old, new in patches:
        n = src.count(old)
        assert n == 1, f"[{tag}] 命中 {n} 次,期望 1 次"
        src = src.replace(old, new, 1)
        print(f"  ok  {tag}")
    return src


s = io.open(P, encoding="utf-8").read()

# ---- 1. /api/publish/finders 带出每个账号的任务计数(账号卡片上的数字) ----
s = apply(s, [(
    "finders 取计数",
    '''    pool = storage.title_pool_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    out = []''',
    '''    pool = storage.title_pool_stats()
    counts = storage.task_counts_by_finder()      # rev58: 账号卡片上的条数
    today = datetime.now().strftime("%Y-%m-%d")
    out = []''',
), (
    "finders 单项加 counts",
    '''            "last_success_at": storage.last_success_at(fid),
            "fail_streak": publish_queue._fail_streak.get(fid, 0),
        })''',
    '''            "last_success_at": storage.last_success_at(fid),
            "fail_streak": publish_queue._fail_streak.get(fid, 0),
            # rev58: {"pending":n, "success":n, ...},前端按页签口径求和显示
            "counts": counts.get(fid) or {},
        })''',
)])

# ---- 2. /api/publish/tasks 透传排序口径 ----
s = apply(s, [(
    "tasks 接口 order",
    '''async def publish_tasks(status: str = "", finder_id: str = "", limit: int = 100, offset: int = 0):
    rows, total = storage.query_tasks(status=status or None, finder_id=finder_id or None,
                                      limit=min(limit, 500), offset=max(0, offset))''',
    '''async def publish_tasks(status: str = "", finder_id: str = "", limit: int = 100,
                         offset: int = 0, order: str = ""):
    """查发布任务。order=queue/history 是【界面】排序(新的在前),
    不传则保持内部调度依赖的旧序(老任务先发),见 storage.query_tasks。"""
    rows, total = storage.query_tasks(status=status or None, finder_id=finder_id or None,
                                      limit=min(limit, 500), offset=max(0, offset),
                                      order=order or None)''',
)])

# ---- 3. /api/system/restart 支持 wait_task:等当前发布任务做完再退出 ----
OLD_RESTART = '''@app.post("/api/system/restart")
async def system_restart(delay: float = Query(3.0, ge=1, le=60)):
    """远程重启:优雅退出 + 按原方式重新拉起(见 backend/restart.py)。

    用法(内网任意机器,无需登录):
        curl -X POST "http://<运行机>:8712/api/system/restart?delay=3"

    调用后【立刻返回】,真正退出发生在 delay 秒之后。客户端应轮询
    /api/system/info:pid 变了且服务可访问 = 重启成功;
    若长时间 pid 不变,看返回的 restart 字段里的 error。
    """
    try:
        info = restart_mod.restart(delay)
    except Exception as e:
        logger.exception("[restart] 启动重启失败")
        raise HTTPException(500, f"重启失败: {e}")
    logger.info(f"[restart] 收到远程重启请求,{delay}s 后退出 "
                f"(helper_pid={info['helper_pid']})")
    return {"ok": True, **info,
            "hint": "本进程将在 delay 秒后退出,请轮询 /api/system/info 确认新 pid"}
'''

NEW_RESTART = '''# rev58: 等待发布任务结束再重启的状态(前端据此提示,避免重启把正在发的作品打断)
_restart_wait = {"waiting": False, "since": 0.0, "max_wait": 0.0}


def _publish_busy():
    """是否有作品正在发布:队列忙碌标志 + 库里 running 状态,两者取或。

    只认其中一个会漏:进程刚接到任务时标志先立、状态后写;异常退出时又只剩状态。
    """
    try:
        if getattr(publish_queue, "_busy", False):
            return True
        return bool(storage.query_tasks(status="running", limit=1)[1])
    except Exception:
        return False


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
    if wait_task and _publish_busy():
        if _restart_wait["waiting"]:
            return {"ok": True, "state": "waiting", "already": True,
                    "waited_sec": round(time.time() - _restart_wait["since"], 1),
                    "max_wait": _restart_wait["max_wait"],
                    "hint": "已经在等当前这一条发完,不用重复点"}
        _restart_wait.update({"waiting": True, "since": time.time(),
                              "max_wait": max_wait})

        async def _restart_after_task():
            waited = 0.0
            try:
                while waited < max_wait:
                    await asyncio.sleep(2.0)
                    waited += 2.0
                    if not _publish_busy():
                        break
                else:
                    logger.warning(f"[restart] 等了 {waited:.0f}s 发布仍未结束,"
                                   f"按 max_wait 强制重启;该任务重启后会被标成待核对")
                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                restart_mod.restart(delay)
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
    logger.info(f"[restart] 收到远程重启请求,{delay}s 后退出 "
                f"(helper_pid={info['helper_pid']}, wait_task={int(bool(wait_task))})")
    return {"ok": True, "state": "restarting", **info,
            "hint": "本进程将在 delay 秒后退出,请轮询 /api/system/info 确认新 pid"}
'''
s = apply(s, [("restart 支持 wait_task", OLD_RESTART, NEW_RESTART)])

# ---- 4. /api/system/info 带出等待状态(排障 + 前端提示) ----
s = apply(s, [(
    "info 带 restart_wait",
    '''    return {"ok": True, **restart_mod.runtime_info(),
            "restart": restart_mod.read_restart_status()}''',
    '''    return {"ok": True, **restart_mod.runtime_info(),
            "restart": restart_mod.read_restart_status(),
            "restart_wait": dict(_restart_wait)}   # rev58: 是否在等发布任务结束''',
)])

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = io.open(P, encoding="utf-8").read()
assert "counts = storage.task_counts_by_finder()" in chk
assert '"counts": counts.get(fid) or {},' in chk
assert "order: str = \"\"" in chk
assert "order=order or None" in chk
assert "wait_task: bool = False" in chk
assert "def _restart_after_task()" in chk
assert '"restart_wait": dict(_restart_wait)' in chk
print("SELFCHECK PASS")
