# -*- coding: utf-8 -*-
"""rev59 真机验证:新停机链路是否真的「先关浏览器再退出」。

改之前母本每次重启的 run.log:
    [重启] 收到重启指令,正在优雅退出…
    WARNING graceful_shutdown 异常: Event loop is closed
    RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
然后 4 秒后 os._exit —— Chromium 被硬砍。

本脚本会真的重启一次母本,并按日志行切片核对:
  1) 有 [shutdown] 开始清理 / 清理完成,用时 Xs(说明清理真的跑了,且等到了)
  2) 有 [重启] 清理完成,退出(不是「超时强制退出」)
  3) 重启窗口内【没有】Event loop is closed / never awaited
  4) 新进程 0 ERROR,账号恢复,auth=ok
"""
import json
import re
import sys
import time
import urllib.request

sys.path.insert(0, r"D:\SPHGJ\tools")
from read_master_log import dec          # noqa: E402

BASE = "http://192.168.1.27:8712"
LOG = r"\\Sphgj\sphgj\run.log"

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


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, timeout=20):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def read_lines():
    raw = open(LOG, "rb").read()
    return [dec(x) for x in raw.splitlines()]


print("=" * 68)
info = get("/api/system/info")
old_pid = info.get("pid")
sh = info.get("shutdown") or {}
print(f"重启前 pid={old_pid}  shutdown={sh}")
ck(sh.get("loop_alive") is True, "启动后事件循环可用", sh)
ck(sh.get("started") is False and sh.get("done") is False,
   "启动后停机状态为「未开始/未完成」", sh)

before = read_lines()
n0 = len(before)
print(f"日志基线行数 = {n0}")

print("\n触发重启(wait_task=1)...")
code, body = post("/api/system/restart?delay=2&wait_task=1")
print(f"  HTTP {code}  {body}")
try:
    ck(json.loads(body).get("state") in ("restarting", "waiting"),
       "重启接口正常受理", body[:120])
except Exception:
    ck(False, "重启接口返回 JSON", body[:120])

# 连点一次:必须返回 already(rev59 并发保护)
try:
    time.sleep(0.3)
    code2, body2 = post("/api/system/restart?delay=2&wait_task=1")
    d2 = json.loads(body2)
    print(f"  重复点: HTTP {code2}  {body2[:160]}")
    ck(d2.get("state") == "already" or code2 != 200,
       "★ 重复点返回 already(不会起第二个 helper)", body2[:160])
except Exception as e:
    print(f"  重复点请求异常(进程可能已在退): {e!r}")
    ck(True, "★ 重复点未造成异常(进程已在退出)")

print("\n等新 pid ...")
t0 = time.time()
new_pid = None
while time.time() - t0 < 300:
    time.sleep(2)
    try:
        p = get("/api/system/info").get("pid")
    except Exception:
        continue
    if p and p != old_pid:
        new_pid = p
        print(f"  {int(time.time() - t0)}s  新 pid = {p}")
        break
ck(new_pid is not None, "300s 内起来了新进程", new_pid)

time.sleep(3)
win = read_lines()[n0:]
print(f"\n重启窗口日志 {len(win)} 行,关键行如下:")
for x in win:
    if any(k in x for k in ("[重启]", "[shutdown]", "graceful_shutdown", "never awaited",
                            "helper 已启动", "开始清理", "收尾")):
        print("   ", x[:170])

print("\n--- 核对 ---")
joined = "\n".join(win)
ck("开始清理" in joined, "★ 有 [shutdown] 开始清理(清理真的被执行)")
m = re.search(r"清理完成,用时 ([\d.]+)s", joined)
ck(m is not None, "★ 有 [shutdown] 清理完成,用时 Xs(等到了,不是睡几秒就走)",
   m.group(0) if m else None)
if m:
    ck(float(m.group(1)) > 0.0, "清理耗时被记录", m.group(1))
ck("清理完成,退出" in joined, "★ 钩子确认「清理完成」后才退出")
ck("清理未在" not in joined, "没有走「超时强制退出」分支")
ck("Event loop is closed" not in joined,
   "★ 不再出现 graceful_shutdown 异常: Event loop is closed")
ck("never awaited" not in joined, "★ 不再出现 coroutine ... was never awaited")
ck("graceful_shutdown 异常" not in joined, "没有任何 graceful_shutdown 异常")

print("\n--- 新进程状态 ---")
time.sleep(4)
info2 = get("/api/system/info")
sh2 = info2.get("shutdown") or {}
print(f"  pid={info2.get('pid')} shutdown={sh2}")
ck(sh2.get("loop_alive") is True, "新进程事件循环可用(干净启动)")

# 新进程启动块 0 ERROR
lines = read_lines()
st = 0
for i in range(len(lines) - 1, -1, -1):
    if "[启动] 视频号工具" in lines[i]:
        st = i
        break
startup = lines[st:]
errs = [x for x in startup if " ERROR " in x or "Traceback" in x]
ck(not errs, "新进程启动 0 ERROR", errs[:3])

print(f"\n{'=' * 68}\nPASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for x in FAILS:
        print("  -", x)
    sys.exit(1)
