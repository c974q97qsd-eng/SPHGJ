# -*- coding: utf-8 -*-
"""等待母本重启后指定账号自动恢复登录（轮询 /api/accounts）。

用法: python wait_accounts_online.py <account_id> [<account_id> ...]
账号名一律由调用方传入，脚本内不预置任何真实账号。
"""
import json
import sys
import time
import urllib.request

BASE = "http://192.168.1.27:8712"
TARGETS = sys.argv[1:]
if not TARGETS:
    print("用法: python wait_accounts_online.py <account_id> [<account_id> ...]")
    sys.exit(2)
DEADLINE = time.time() + 180


def get(path, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    last = {}
    while time.time() < DEADLINE:
        try:
            st = get("/api/accounts")
            last = {a.get("id"): a for a in (st.get("accounts") or [])}
            if all((last.get(t) or {}).get("logged_in") for t in TARGETS):
                print("ALL ONLINE  elapsed~%ds" % (180 - int(DEADLINE - time.time())))
                break
        except Exception as e:
            print("  [wait] %s" % type(e).__name__)
        time.sleep(5)
    for t in TARGETS:
        a = last.get(t) or {}
        print("  %-14s logged_in=%-5s auth_state=%-7s reason=%s" % (
            t, a.get("logged_in"), a.get("auth_state"), (a.get("auth_reason") or "")[:50]))
    ok = all((last.get(t) or {}).get("logged_in") for t in TARGETS)
    print("RESULT: %s" % ("PASS" if ok else "TIMEOUT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
