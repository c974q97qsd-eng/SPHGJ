# -*- coding: utf-8 -*-
"""rev58b 单测:重启接口的 wait_task 分支(等当前发布任务做完再退)。

为什么值得单测:用户点「重启」最常见的心态就是「别把我正在发的作品搞坏」。
这条分支只在「有任务在发」时才会走到 —— 恰恰最难手测,一旦里面写错(变量名/流程),
表现就是「点了没反应」,而那正是最不该出错的时刻。

跑法:python tools/test_rev58b_restart_wait.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 本机没装 playwright(只在母本),stub 掉只为能 import server 链路
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


for _name in ("playwright", "playwright.async_api", "playwright._impl",
              "playwright._impl._errors"):
    sys.modules.setdefault(_name, _StubMod(_name))
sys.modules["playwright"].async_api = sys.modules["playwright.async_api"]


# 本机也没装 fastapi(只在母本)—— 用「什么都能当装饰器/什么都能取属性」的万能桩,
# 只为让 server 模块 import 得下去;被测函数本身是真的(含 Query 默认值由调用方显式传)。
class _AnyMeta(type):
    def __getattr__(cls, name):
        return _Any


class _Any(metaclass=_AnyMeta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        # 装饰器用法:@app.get(...) 返回的桩再被「单个函数」调用时,原样返回该函数
        if len(a) == 1 and not k and callable(a[0]):
            return a[0]
        return _Any()

    def __getattr__(self, name):
        return _Any()


def _stub_pkg(name):
    m = types.ModuleType(name)

    def _ga(n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _Any

    m.__getattr__ = _ga
    sys.modules.setdefault(name, m)
    return sys.modules[name]


for _name in ("fastapi", "fastapi.responses", "fastapi.staticfiles",
              "fastapi.middleware", "fastapi.middleware.cors",
              "pydantic", "pydantic.fields", "pydantic.main"):
    _stub_pkg(_name)

import backend.server as S       # noqa: E402

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


# 打桩:重启动作与「是否在发」都由用例控制,绝不真的重启
CALLS = []


def fake_restart(delay=3.0):
    CALLS.append(delay)
    return {"helper_pid": 12345, "mode": "stub"}


S.restart_mod.restart = fake_restart
BUSY = {"v": False}
S.publish_queue.busy_now = lambda: BUSY["v"]


def reset_state():
    CALLS.clear()
    BUSY["v"] = False
    S._restart_wait.update({"waiting": False, "since": 0.0, "max_wait": 0.0})


# ==================== 1. 空闲:点了就重启 ====================
print("\n[1] 没有任务在发 -> 立刻重启(state=restarting)")


async def t_idle():
    reset_state()
    return await S.system_restart(delay=1, wait_task=True, max_wait=5)


r = asyncio.run(t_idle())
ck(r["state"] == "restarting", "state=restarting", r.get("state"))
ck(r.get("ok") is True, "ok=True")
ck(CALLS == [1], "重启动作被调用一次且带上了 delay", CALLS)
ck(S._restart_wait["waiting"] is False, "没有挂起等待状态")


# ==================== 2. 有任务在发:先等,发完再重启 ====================
print("\n[2] 有作品正在发布 -> state=waiting,发完才真的重启")


async def t_wait():
    reset_state()
    BUSY["v"] = True
    r1 = await S.system_restart(delay=0.5, wait_task=True, max_wait=10)
    called_immediately = list(CALLS)
    waiting_flag = S._restart_wait["waiting"]
    # 重复点:不能起第二个等待任务
    r2 = await S.system_restart(delay=0.5, wait_task=True, max_wait=10)
    # 模拟这条作品发完
    BUSY["v"] = False
    await asyncio.sleep(3.0)          # wait_idle 的 sleep 是 2s,给它一拍
    return r1, r2, called_immediately, waiting_flag


r1, r2, called_immediately, waiting_flag = asyncio.run(t_wait())
ck(r1["state"] == "waiting", "第一次:state=waiting", r1.get("state"))
ck("发布" in (r1.get("hint") or ""), "带出人话提示", r1.get("hint"))
ck(called_immediately == [], "等待期间【没有】调用重启(不会打断正在发的那条)", called_immediately)
ck(waiting_flag is True, "等待状态已挂起(前端可据此提示)")
ck(r2["state"] == "waiting" and r2.get("already") is True,
   "重复点:返回 already=True,不叠加第二个等待任务", r2)
ck(CALLS == [0.5], "★ 任务发完后自动重启了一次(等到了才走)", CALLS)
ck(S._restart_wait["waiting"] is False, "等待状态已复位")


# ==================== 3. 一直在发:到 max_wait 强走 ====================
print("\n[3] 一直发不完 -> 到 max_wait 也要走,不能把重启按钮变成哑巴")


async def t_cap():
    reset_state()
    BUSY["v"] = True
    r = await S.system_restart(delay=0.5, wait_task=True, max_wait=0.4)
    await asyncio.sleep(2.2)          # 等后台任务跑完(它内部先 sleep 2s)
    return r, list(CALLS)


r, calls = asyncio.run(t_cap())
ck(r["state"] == "waiting", "仍然先返回 waiting", r.get("state"))
ck(calls == [0.5], "★ 即使任务没结束,到 max_wait 后仍旧重启(不会无限期挂着)", calls)


# ==================== 4. wait_task 默认关闭:保持旧行为 ====================
print("\n[4] 不传 wait_task(工具脚本/curl 的习惯用法)-> 立刻重启,行为不变")


async def t_legacy():
    reset_state()
    BUSY["v"] = True                # 即使正在发
    r = await S.system_restart(delay=3, wait_task=False, max_wait=10)
    return r


r = asyncio.run(t_legacy())
ck(r["state"] == "restarting", "state=restarting", r.get("state"))
ck(CALLS == [3], "旧调用方(delay=3)行为完全没变", CALLS)
ck(S._restart_wait["waiting"] is False, "不进入等待分支")


# ==================== 5. /api/system/info 带出等待状态 ====================
print("\n[5] /api/system/info 带出 restart_wait(排障可见)")


async def t_info():
    reset_state()
    BUSY["v"] = True
    await S.system_restart(delay=1, wait_task=True, max_wait=10)
    info = await S.system_info()
    BUSY["v"] = False
    await asyncio.sleep(2.6)
    return info


info = asyncio.run(t_info())
ck("restart_wait" in info, "info 里有 restart_wait 字段", list(info)[:6])
ck(isinstance(info.get("restart_wait"), dict), "restart_wait 是 dict")
ck("pid" in info, "原有的 pid 字段没被弄丢")

print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
