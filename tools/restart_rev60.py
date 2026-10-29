# -*- coding: utf-8 -*-
"""重启母本并等新 pid 起来(rev60 第一次:让新代码生效)。

投递完成后的第一次重启,收尾执行的仍是【母本上的新代码】——
所以这次 restart 本身也是一次 rev59 停机链路的复核。
"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"


def get(path, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post(path, timeout=15):
    req = urllib.request.Request(BASE + path, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


old = get("/api/system/info")
old_pid = old.get("pid")
print(f"重启前 pid = {old_pid}  version={old.get('version')}")
print(f"shutdown 状态 = {old.get('shutdown')}")

print("\n触发重启 wait_task=1 ...")
try:
    code, body = post("/api/system/restart?delay=3&wait_task=1")
    print(f"  HTTP {code}  {body}")
except Exception as e:
    print(f"  触发请求异常(进程可能已退出,继续轮询): {e!r}")

print("\n等新 pid ...")
t0 = time.time()
new_pid = None
while time.time() - t0 < 300:
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
    print("FAIL: 300s 内没等到新 pid")
    sys.exit(1)

print(f"\n重启完成: {old_pid} -> {new_pid}")
