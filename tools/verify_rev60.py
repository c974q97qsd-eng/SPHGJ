# -*- coding: utf-8 -*-
"""rev60 真机验证:在「账号恢复进行中」触发重启,看会不会再把好账号踢掉。

母本现场(01:19:46 同一秒)曾经是:
    [shutdown] 开始清理(thread):账号进程 0 个      <- 恢复进行中,workers 是空的
    ERROR  [acc2] 启动开浏览器失败: …Target page…closed
    WARNING [恢复] acc2 未登录或启动失败,已移出恢复列表
    -> auth_state.json 被写成 offline + run_state.json 丢掉该账号

本脚本做两轮重启:
  第 1 轮:把进程带到干净的起点
  第 2 轮:卡在「启动后 ~18s、恢复刚开始启动前几个账号」的窗口内触发 ——
          这正是现场的时机。然后逐项核对上面每一环都不再发生。

跑法:python tools/verify_rev60.py
"""
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec        # noqa: E402

BASE = "http://192.168.1.27:8712"
LOG = r"\\Sphgj\sphgj\run.log"
DATA = r"\\Sphgj\sphgj\data"

PASS = FAIL = 0
FAILS = []


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name + (" | " + str(extra) if extra else ""))
        print(f"  FAIL  {name}  {extra}")


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, timeout=20):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def log_lines():
    with open(LOG, "rb") as f:
        raw = f.read()
    return [dec(x) for x in raw.splitlines()]


def read_json(name):
    p = os.path.join(DATA, name)
    try:
        with io.open(p, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def intent_set():
    d = read_json("run_state.json") or {}
    return set(d.get("running") or [])


def offline_map():
    return dict(read_json("auth_state.json") or {})


def uptime_sec():
    """从 started_at 反推进程已启动多少秒(uptime_sec 字段不可靠)。"""
    i = get("/api/system/info")
    st = i.get("started_at") or ""
    try:
        t = datetime.strptime(st, "%Y-%m-%dT%H:%M:%S").timestamp()
        return time.time() - t, i.get("pid")
    except Exception:
        return -1, i.get("pid")


def restart_and_wait(old_pid, max_wait=300):
    try:
        post("/api/system/restart?delay=3&wait_task=1")
    except Exception as e:
        print(f"    触发重启返回异常(进程可能已退): {e!r}")
    t0 = time.time()
    while time.time() - t0 < max_wait:
        time.sleep(1)
        try:
            pid = get("/api/system/info").get("pid")
        except Exception:
            continue
        if pid and pid != old_pid:
            return pid, int(time.time() - t0)
    return None, int(time.time() - t0)


print("=" * 70)
print("rev60 真机验证:恢复途中重启")
print("=" * 70)

# ---------- 第 1 轮:带到干净起点 ----------
up, pid0 = uptime_sec()
print(f"\n[准备] 当前 pid={pid0} 已运行 {up:.0f}s")
if up < 60:
    print("  刚重启过,直接等恢复窗口")
    pid1 = pid0
    t_start = time.time() - up
else:
    print("  先重启一次拿到干净的启动时刻 ...")
    pid1, cost = restart_and_wait(pid0)
    if not pid1:
        ck("能拿到新 pid", False, "300s 未起来")
        sys.exit(1)
    t_start = time.time()
    print(f"  新 pid={pid1}({cost}s)")

# ---------- 等进入「恢复刚开始」的窗口 ----------
# 恢复流程:启动 -> heal_login_profiles -> sleep 15s -> 逐个 start_account(gap 4s)
# 落在 17~24s 之间发重启,正好在启动前 1~2 个账号。
target = 19.0
now_off = time.time() - t_start
if now_off < target:
    wait = target - now_off
    print(f"\n[等窗口] 再等 {wait:.0f}s,让恢复刚好开始启动第一批账号")
    time.sleep(wait)
else:
    print(f"\n[等窗口] 已运行 {now_off:.0f}s(比目标晚),仍然继续")

up, pid = uptime_sec()
acc = get("/api/accounts").get("accounts") or []
run_now = [x.get("id") for x in acc if x.get("running")]
print(f"  此刻 pid={pid} 已运行 {up:.0f}s, running={len(run_now)} 个 {run_now}")

# ---------- 打点 + 快照 ----------
lines_before = len(log_lines())
intent_before = intent_set()
offline_before = offline_map()
print(f"  重启前快照:运行意图 {sorted(intent_before)}")
print(f"             离线记录 {sorted(offline_before)}")

# ---------- 第 2 轮:在恢复途中重启 ----------
print("\n[关键] 在恢复途中触发重启 ...")
pid2, cost = restart_and_wait(pid)
if not pid2:
    ck("恢复途中重启后能起来", False, "300s 未起来")
    sys.exit(1)
print(f"  新 pid = {pid2}  ({cost}s)")

# ---------- 看这次停机块的日志 ----------
time.sleep(3)
lines_after = log_lines()
seg = lines_after[lines_before:]        # 本次重启请求之后的全部日志
head = seg[:220]

# 找到 shutdown 块
sh_blocks = [i for i, x in enumerate(head) if "开始清理" in x]
clean_start = head[sh_blocks[0]] if sh_blocks else ""
print(f"\n[1] 本次停机日志: {clean_start[:110] if clean_start else '(没找到)'}")

ck("★ 清理日志里报出了「启动中 N 个」(以前只报 workers=0,看不出在恢复途中)",
   "启动中" in clean_start, clean_start)
m = re.search(r"启动中\s*(\d+)\s*个", clean_start)
starting_n = int(m.group(1)) if m else -1
ck("★ 这次重启真的撞上了「启动中」的账号(证明现场复现成功)",
   starting_n >= 1, f"启动中={starting_n}")

block = "\n".join(head[sh_blocks[0]:sh_blocks[0] + 60]) if sh_blocks else ""
ck("★ 没有「已移出恢复列表」(现场就有这一条)", "已移出恢复列表" not in block,
   [x[:110] for x in head if "已移出恢复列表" in x][:3])
ck("★ 没有把好账号记成离线(启动时 auth_data 硬验未通过)",
   "auth_data 硬验未通过" not in block,
   [x[:110] for x in head if "硬验未通过" in x][:3])
# 收手有两种形态,至少要有一种:
#   a) 停机撞在 sleep 间隙 -> 恢复循环自己打「正在停机,中止自动恢复」
#   b) 停机撞在 start_account 内部 -> stop_all 主动关掉「启动中」的 worker,
#      与此同时恢复循环的 task 被 uvicorn 一并取消,来不及打日志(这是最优路径)
_quit_a = any(k in block for k in ("正在停机", "放弃本次启动", "启动过程中进入停机"))
_quit_b = ("关闭" in block and "正在启动中的账号进程" in block)
ck("停机时对「启动中」的账号做了收手(关掉它 / 中止恢复)", _quit_a or _quit_b,
   [x[:110] for x in head if "停机" in x or "启动中" in x][:4])
ck("★ 主动关掉了正在启动中的账号进程(以前这批人完全没人管)", _quit_b,
   [x[:110] for x in head if "启动中" in x][:3])
ck("没有旧故障 Event loop is closed", "Event loop is closed" not in block,
   [x[:110] for x in head if "Event loop is closed" in x][:2])
ck("没有 never awaited 警告", "never awaited" not in block,
   [x[:110] for x in head if "never awaited" in x][:2])
ck("日志里出现 rev59 的「清理完成,用时」",
   any("清理完成,用时" in x for x in head),
   [x[:110] for x in head if "清理完成" in x][:2])

# ---------- 状态文件核对 ----------
print("\n[2] 状态文件核对")
intent_after = intent_set()
offline_after = offline_map()
print(f"  重启后运行意图 {sorted(intent_after)}")
print(f"  重启后离线记录 {sorted(offline_after)}")

ck("★★ run_state.json 的运行意图一个没丢(★ 现场会丢一个)",
   intent_before <= intent_after,
   f"丢了 {sorted(intent_before - intent_after)}")

new_off = set(offline_after) - set(offline_before)
ck("★★ auth_state.json 没被新增污染(★ 现场会多一条)",
   not new_off, f"新增 {sorted(new_off)}")

changed = []
for k in set(offline_before) & set(offline_after):
    if (offline_before[k] or {}).get("reason") != (offline_after[k] or {}).get("reason"):
        changed.append(k)
ck("已有离线记录的原因没被改写", not changed, changed)

# ---------- 等新进程把账号拉起来 ----------
print("\n[3] 等新进程自动恢复账号(最多 180s) ...")
t0 = time.time()
last = -1
while time.time() - t0 < 180:
    acc = get("/api/accounts").get("accounts") or []
    run = [x for x in acc if x.get("running")]
    if len(run) != last:
        last = len(run)
        print(f"  {int(time.time() - t0):3d}s  running={len(run)}/{len(acc)}")
    if len(run) >= len(intent_after) and len(run) > 0:
        break
    time.sleep(5)

acc = get("/api/accounts").get("accounts") or []
run = [x for x in acc if x.get("running")]
ok = [x for x in run if x.get("auth_state") == "ok"]
print(f"  最终 running={len(run)}/{len(acc)}  auth=ok {len(ok)}")
ck("新进程把账号重新拉起来了", len(run) >= min(len(intent_after), 1), len(run))
ck("恢复后的账号登录态正常(auth_state=ok)", len(ok) == len(run),
   [(x.get("id"), x.get("auth_state")) for x in run if x.get("auth_state") != "ok"])

# 日志 ERROR 统计(本次启动至今)
lines_now = log_lines()
st = 0
for i in range(len(lines_now) - 1, -1, -1):
    if "评论归属迁移" in lines_now[i]:
        st = i
        break
tail = lines_now[st:]
errs = [x for x in tail if " ERROR " in x or "Traceback" in x]
print(f"\n[4] 本次启动日志 {len(tail)} 行,ERROR {len(errs)} 条")
for x in errs[:6]:
    print("    ", x[:150])
ck("新进程启动 0 ERROR", not errs, len(errs))

print("\n" + "=" * 70)
print(f"PASS {PASS}   FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for x in FAILS:
        print("  -", x)
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
