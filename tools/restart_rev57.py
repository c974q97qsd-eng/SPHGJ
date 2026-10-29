# -*- coding: utf-8 -*-
"""重启母本并等新 pid 起来。

POST /api/system/restart?delay=3 之后进程会退出再由守护拉起,pid 必变。
轮询 /api/system/info 直到 pid 变化 + 接口可用,最多等 150s。
"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"


def get(path, timeout=6):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, timeout=10):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")[:200]


try:
    old = get("/api/system/info")
except Exception as e:
    print(f"取旧 pid 失败: {e!r}")
    sys.exit(1)
old_pid = old.get("pid")
print(f"重启前 pid = {old_pid}  version={old.get('version')}")

print("\n触发重启 ...")
try:
    code, body = post("/api/system/restart?delay=3")
    print(f"  HTTP {code}  {body}")
except Exception as e:
    print(f"  触发请求异常(进程可能已退出,继续轮询): {e!r}")

print("\n等新 pid ...")
t0 = time.time()
new_pid = None
while time.time() - t0 < 150:
    time.sleep(2)
    try:
        info = get("/api/system/info")
    except Exception:
        continue
    pid = info.get("pid")
    if pid and pid != old_pid:
        new_pid = pid
        print(f"  {int(time.time() - t0)}s  新 pid = {pid}")
        break

if not new_pid:
    print("FAIL: 150s 内没等到新 pid")
    sys.exit(1)

print("\n等服务就绪 ...")
for _ in range(20):
    time.sleep(2)
    try:
        info = get("/api/system/info")
        accts = get("/api/accounts") if False else None
        print(f"  pid={info.get('pid')}  version={info.get('version')}  "
              f"uptime={info.get('uptime_sec') or info.get('uptime')}")
        break
    except Exception:
        continue

print(f"\n重启完成: {old_pid} -> {new_pid}")
