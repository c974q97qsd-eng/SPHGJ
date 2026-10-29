# -*- coding: utf-8 -*-
"""重启母本并等新 pid 起来(rev59)。

注意:执行这次重启的是【母本上正在跑的旧代码】,所以它这次的收尾仍是旧行为
(必然再打一次 Event loop is closed)。rev59 的停机链路要等下一次重启才生效 ——
verify_rev59.py 会再做一次重启来验证。
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
print(f"restart_wait = {old.get('restart_wait')}")
print(f"shutdown 状态 = {old.get('shutdown')}  (老版本没有这个字段)")

busy = None
try:
    st = get("/api/publish/status")
    busy = bool(st.get("busy")) or bool(st.get("running"))
    print(f"当前发布忙碌 = {busy}  (pending={st.get('pending')} running={st.get('running')})")
except Exception as e:
    print(f"取发布状态失败(不影响重启): {e!r}")

print("\n触发重启 wait_task=1 ...")
try:
    code, body = post("/api/system/restart?delay=3&wait_task=1")
    print(f"  HTTP {code}  {body}")
    try:
        d = json.loads(body)
        st = d.get("state")
        print(f"  返回 state = {st}  " +
              ("(有任务在发,会等它发完)" if st == "waiting" else "(立刻重启)"))
    except Exception:
        pass
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

print("\n等服务就绪 ...")
for _ in range(20):
    time.sleep(2)
    try:
        info = get("/api/system/info")
        print(f"  pid={info.get('pid')}  version={info.get('version')}  "
              f"shutdown={info.get('shutdown')}")
        break
    except Exception:
        continue

print(f"\n重启完成: {old_pid} -> {new_pid}")
