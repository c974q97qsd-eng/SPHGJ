# -*- coding: utf-8 -*-
"""轮询母本 /api/system/info,确认重启后新 pid 并输出账号在网状态。"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"
OLD = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


deadline = time.time() + 180
info = None
while time.time() < deadline:
    try:
        info = get("/api/system/info")
        if info.get("pid") and info["pid"] != OLD:
            break
    except Exception:
        pass
    time.sleep(3)

if not info or info.get("pid") == OLD:
    print("FAIL: 重启后 pid 未变化")
    sys.exit(1)

print("新 pid=%s  started_at=%s  version=%s" % (info["pid"], info.get("started_at"), info.get("version")))
print("restart:", json.dumps(info.get("restart") or {}, ensure_ascii=False))

try:
    st = get("/api/accounts")
except Exception as e:
    print("engine status 取不到:", e)
    sys.exit(0)

print("running=%s" % st.get("running"))
for a in st.get("accounts") or []:
    print("  %-12s logged_in=%-5s auth_state=%-7s reason=%s" % (
        a.get("id"), a.get("logged_in"), a.get("auth_state"), (a.get("auth_reason") or "")[:40]))
