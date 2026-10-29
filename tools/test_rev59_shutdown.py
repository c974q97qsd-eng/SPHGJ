# -*- coding: utf-8 -*-
"""rev59 单测:停机链路(清理只跑一次 + 一定在事件循环里跑)+ 并发重启保护。

为什么值得单测:
    改之前每次重启的 run.log 都是这两行:
        WARNING graceful_shutdown 异常: Event loop is closed
        RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
    也就是【清理一次都没跑成】,随后 os._exit 把 Chromium 硬砍。这条路径只在
    退出时走,平时手测根本看不到 —— 只能靠单测把"一定会执行、只执行一次"钉死。

跑法:
    python tools/test_rev59_shutdown.py          # 普通解释器(无 fastapi 时自动打桩)
    配置环境安装/Python314/python.exe tools/test_rev59_shutdown.py   # 带真 uvicorn 跑集成段
"""
import asyncio
import gc
import io
import os
import sys
import threading
import time
import types
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def ck(cond, name, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra is not None else ""))


# ---------------- 依赖打桩 ----------------
# 本机默认解释器没装 playwright / fastapi(只在母本),不桩就 import 不进 server 链路。
def _has(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


HAS_FASTAPI = _has("fastapi") and _has("uvicorn")


class _StubMod(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.endswith("Error"):
            v = type(name, (Exception,), {})
        elif name[0].isupper():
            v = type(name, (), {})
        else:
            def v(*a, **k):
                return None
        setattr(self, name, v)
        return v


if not _has("playwright"):
    for _n in ("playwright", "playwright.async_api", "playwright._impl",
               "playwright._impl._errors"):
        sys.modules.setdefault(_n, _StubMod(_n))
    sys.modules["playwright"].async_api = sys.modules["playwright.async_api"]


class _AnyMeta(type):
    def __getattr__(cls, name):
        return _Any


class _Any(metaclass=_AnyMeta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        if len(a) == 1 and not k and callable(a[0]):
            return a[0]
        return _Any()

    def __getattr__(self, name):
        return _Any()


def _stub_pkg(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda n: _Any
    sys.modules.setdefault(name, m)


if not HAS_FASTAPI:
    for _n in ("fastapi", "fastapi.responses", "fastapi.staticfiles",
               "fastapi.middleware", "fastapi.middleware.cors",
               "pydantic", "pydantic.fields", "pydantic.main"):
        _stub_pkg(_n)

import backend.server as S            # noqa: E402
import backend.restart as R          # noqa: E402


# ==================== 1. _stop_everything 只跑一次 ====================
print("\n[1] _stop_everything:重复调用只清理一次")


class FakeMgr:
    def __init__(self, delay=0.05, boom=None):
        self.n = 0
        self.delay = delay
        self.boom = boom
        self.workers = {}

    async def stop_all(self):
        self.n += 1
        await asyncio.sleep(self.delay)
        if self.boom:
            raise self.boom


def reset_shutdown():
    S._shutdown.update(started=False, done=False, started_at=0.0,
                       elapsed=0.0, path="", error="")
    S._shutdown_done.clear()
    S._stop_task = None          # 共享清理任务也要复位,否则下一段复用到已完成的 task


reset_shutdown()
fm = FakeMgr()
S.manager = fm


async def t_twice():
    await S._stop_everything("unit")
    await S._stop_everything("unit")      # 第二次必须直接返回
    return S._shutdown_done.is_set()


got = asyncio.run(t_twice())
ck(fm.n == 1, "★ stop_all 只被调用 1 次(第二次不重复关浏览器)", fm.n)
ck(got is True, "完成标志已置位")
ck(S._shutdown["done"] is True, "done=True")
ck(S._shutdown["path"] == "unit", "记录触发来源", S._shutdown["path"])
ck(S._shutdown["elapsed"] >= 0.05, "记录了耗时", S._shutdown["elapsed"])


# ==================== 2. 清理抛异常也不能卡住 ====================
print("\n[2] 清理抛异常 -> 仍然置完成标志(否则调用方干等到超时)")

reset_shutdown()
S.manager = FakeMgr(boom=RuntimeError("模拟 context.close 失败"))
asyncio.run(S._stop_everything("unit"))
ck(S._shutdown_done.is_set(), "异常后完成标志仍置位")
ck("模拟 context.close 失败" in (S._shutdown["error"] or ""),
   "异常写进 error(排障可见)", S._shutdown["error"])


# ==================== 3. 事件循环已关闭:不抛 never-awaited ====================
print("\n[3] 事件循环已关闭 -> 不排协程、不产生 never-awaited 警告")


def t_dead_loop():
    reset_shutdown()
    S.manager = FakeMgr()
    dead = asyncio.new_event_loop()
    dead.close()
    old_loop = S._LOOP
    S._LOOP = dead
    try:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            ok = S.graceful_shutdown(timeout=0.6)
            gc.collect()
            time.sleep(0.05)
            gc.collect()
        msgs = [str(x.message) for x in w]
        return ok, msgs
    finally:
        S._LOOP = old_loop


ok, msgs = t_dead_loop()
ck(ok is False, "循环已死时返回 False(不谎报清理完成)", ok)
ck(not any("never awaited" in m for m in msgs),
   "★ 不再出现 'coroutine ... was never awaited'(历史噪音)", msgs[:3])
ck(S._shutdown["done"] is False, "没有假装清理过")


# ==================== 4. graceful_shutdown 幂等 + wait_shutdown ====================
print("\n[4] graceful_shutdown 幂等 / wait_shutdown 只等不触发")

reset_shutdown()
ck(S.wait_shutdown(0.05) is False, "未清理时 wait_shutdown 返回 False")
ck(S._shutdown["started"] is False, "wait_shutdown 不会自己触发清理")
S._shutdown_done.set()
ck(S.graceful_shutdown(0.1) is True, "已完成时 graceful_shutdown 立刻返回 True")
ck(S.graceful_shutdown(0.1) is True, "重复调用仍 True(幂等)")


# ==================== 5. restart 并发保护 ====================
print("\n[5] restart():120s 内重复请求不再拉起第二个 helper")

SPAWNED = []
R.spawn_helper = lambda delay=3.0, ports=None: (SPAWNED.append(delay) or 99999)
R.request_exit = lambda delay=3.0: True
_real_status = R.STATUS_PATH
R.STATUS_PATH = os.path.join(ROOT, "data", "_test_rev59_restart_status.json")

try:
    R._inflight.update(at=0.0, helper_pid=0)
    r1 = R.restart(3.0)
    r2 = R.restart(3.0)
    r3 = R.restart(2.0)
    ck(len(SPAWNED) == 1, "★ 连点 3 次只拉起 1 个 helper", SPAWNED)
    ck(r1.get("already") in (None, False), "第一次是正常重启", r1)
    ck(r2.get("already") is True and r3.get("already") is True,
       "第 2/3 次返回 already=True", (r2.get("already"), r3.get("already")))
    ck(r2.get("helper_pid") == 99999, "already 时回带原始 helper_pid", r2.get("helper_pid"))
    # 过了窗口期应当恢复
    R._inflight["at"] = time.time() - R.INFLIGHT_WINDOW - 1
    r4 = R.restart(3.0)
    ck(r4.get("already") in (None, False) and len(SPAWNED) == 2,
       "窗口期过后可以再次重启", SPAWNED)
finally:
    R.STATUS_PATH = _real_status
    R._inflight.update(at=0.0, helper_pid=0)
    try:
        os.remove(os.path.join(ROOT, "data", "_test_rev59_restart_status.json"))
    except OSError:
        pass


# ==================== 6. helper 源码 ====================
print("\n[6] 内嵌 helper:先确认没有别的新实例,再拉")

helper = R.HELPER_SOURCE
try:
    compile(helper, "HELPER_SOURCE", "exec")
    ck(True, "HELPER_SOURCE 语法可编译")
except SyntaxError as e:
    ck(False, "HELPER_SOURCE 语法可编译", e)
ck("def running_pid(ports):" in helper, "新增 running_pid 探测")
ck("跳过拉起" in helper, "发现已有实例则跳过拉起")
ck("deadline = time.time() + 75" in helper, "等旧进程上限提到 75s(不再误杀收尾中的进程)")
ck(helper.index("deadline = time.time() + 75") < helper.index("port0, pid0 = running_pid(ports)"),
   "先等旧进程退出,再判断外面有没有新实例")
ck("phase=\"done\"" in helper and "new_pid=pid0" in helper,
   "认领已有实例时如实回写 new_pid(远程轮询才不会误判失败)")


# ==================== 7. main.py 退出顺序(静态) ====================
print("\n[7] main.py:先停机等清理 -> 再关窗口 -> 才退出")

src = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
i_stop = src.index("ok = _stop_backend()")
i_destroy = src.index('_win["obj"].destroy()\n            except')
ck(i_stop < i_destroy, "清理等待在关窗口之前(不提前杀 UI,也不提前断连接)")
ck("time.sleep(4.0)" not in src, "不再用固定 4 秒顶替清理等待")
ck("timeout_graceful_shutdown=DRAIN_TIMEOUT_SEC" in src,
   "uvicorn 排空连接有上限(否则走不到 lifespan shutdown)")
ck("GRACEFUL_TIMEOUT = 20.0" in src, "清理等待上限 20s")
ck(src.count("print(") == 1, "所有输出走带时间戳的 _p()", src.count("print("))
ck("server.graceful_shutdown()\n" not in src, "不再有无参直呼(旧路径清空)")
ck(src.count("_stop_backend()") >= 5, "_stop_backend 覆盖所有退出路径",
   src.count("_stop_backend()"))


# ==================== 8. 真 uvicorn 端到端(需要 fastapi/uvicorn) ====================
print("\n[8] 真 uvicorn:should_exit -> lifespan shutdown -> 清理真的执行")


def _start_server(port, mgr):
    """起一个真 uvicorn(真 lifespan),返回 (server, thread)。"""
    import uvicorn
    import urllib.request

    S.manager = mgr
    S.sweep_stale_view_profiles = lambda: []
    S.publish_queue.start = lambda: None          # 别让测试触发真实发布调度
    cfg = uvicorn.Config(S.app, host="127.0.0.1", port=port,
                         log_level="warning", access_log=False,
                         timeout_graceful_shutdown=8.0)
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(150):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system/info", timeout=0.5).read()
            return srv, th, True
        except Exception:
            time.sleep(0.1)
    return srv, th, False


def run_integration():
    # --- 场景 A:main.py 重启钩子的顺序(先 poke should_exit,再等清理) ---
    reset_shutdown()
    fm2 = FakeMgr(delay=0.4)
    srv, th, ready = _start_server(8791, fm2)
    if not ready:
        return {"ready": False}
    srv.should_exit = True
    ok1 = S.graceful_shutdown(timeout=15)
    ok2 = S.graceful_shutdown(timeout=2)          # 幂等:第二次立刻 True
    th.join(10)
    return {"ready": True, "ok1": ok1, "ok2": ok2, "n": fm2.n,
            "alive": th.is_alive(), "path": S._shutdown["path"],
            "err": S._shutdown["error"], "elapsed": S._shutdown["elapsed"]}


def run_lifespan_only():
    """场景 B:没人从线程触发清理,uvicorn 自己退出(Ctrl+C / 服务自己结束)。

    这条路径靠的是 lifespan shutdown —— 只要它没挂上,清理就永远不会发生,
    而现场表现只是"浏览器残留",很难联想到是这里。
    """
    reset_shutdown()
    fm3 = FakeMgr(delay=0.3)
    srv, th, ready = _start_server(8792, fm3)
    if not ready:
        return {"ready": False}
    srv.should_exit = True
    th.join(15)
    return {"ready": True, "n": fm3.n, "alive": th.is_alive(),
            "path": S._shutdown["path"], "done": S._shutdown_done.is_set(),
            "err": S._shutdown["error"], "elapsed": S._shutdown["elapsed"]}


if not HAS_FASTAPI:
    print("  SKIP 本机没有 fastapi/uvicorn(用母本同版本 Python314 跑本文件即可覆盖)")
else:
    r = run_integration()
    if not r.get("ready"):
        ck(False, "测试用 uvicorn 起来了", r)
    else:
        ck(r["ok1"] is True, "★ graceful_shutdown 返回 True(清明确实完成)", r)
        ck(r["n"] == 1, "★ stop_all 恰好执行 1 次(不是 0 次,也不重复)", r["n"])
        ck(r["ok2"] is True, "重复调用幂等", r["ok2"])
        ck(r["alive"] is False, "uvicorn 线程已退出(服务真的停了)")
        ck(r["path"] in ("thread", "lifespan"), "清理在事件循环里执行", r["path"])
        ck(not r["err"], "无清理异常", r["err"])
        ck(r["elapsed"] >= 0.4,
           "★ 等的是「清理真跑完」而不是「睡几秒就走」"
           "(elapsed 必须 >= stop_all 自己的耗时 —— 之前 0.13s 说明被取消了)",
           r["elapsed"])

    print("\n[9] 没人触发清理、uvicorn 自己退出 -> lifespan shutdown 兜住")
    r2 = run_lifespan_only()
    if not r2.get("ready"):
        ck(False, "第二段 uvicorn 起来了", r2)
    else:
        ck(r2["alive"] is False, "uvicorn 已退出")
        ck(r2["done"] is True, "★ 清理仍被执行(done 置位)", r2)
        ck(r2["n"] == 1, "★ stop_all 执行 1 次", r2["n"])
        ck(r2["path"] == "lifespan", "触发来源 = lifespan", r2["path"])
        ck(r2["elapsed"] >= 0.3, "lifespan 一直等到清理结束才返回", r2["elapsed"])
        ck(not r2["err"], "无清理异常", r2["err"])



print(f"\n{'=' * 60}\nPASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for x in FAILS:
        print("  -", x)
    sys.exit(1)
