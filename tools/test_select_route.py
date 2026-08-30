"""测试 select-account 路由(不消耗扫码、不依赖 httpx)。

用手搓的 ASGI 调用直接打 FastAPI app,替换 server.manager 为桩对象。
覆盖: 正常选择 / 未知 sid / 点击失败 / 缺省 index / 非数字 index。
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from backend import server  # noqa: E402


class FakeSess:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def select_account(self, index):
        self.calls.append(index)
        return self.ok


class FakeMgr:
    """复刻 AccountManager.select_login 的转发语义(未知 sid / 选择失败都返回 False)。"""

    def __init__(self):
        self.login_sessions = {}

    async def select_login(self, sid, index=0):
        s = self.login_sessions.get(sid)
        if not s:
            return False
        return await s.select_account(index)


async def call(method, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "scheme": "http", "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode())],
    }
    inbox = [{"type": "http.request", "body": raw, "more_body": False}]
    sent = []

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    await server.app(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        payload = json.loads(out) if out else None
    except Exception:
        payload = out
    return status, payload


PASS = FAIL = 0


def check(ok, label, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


async def main():
    mgr = FakeMgr()
    server.manager = mgr
    print("=== select-account 路由 ===")

    s = FakeSess(ok=True)
    mgr.login_sessions["sid-ok"] = s
    st, body = await call("POST", "/api/accounts/login/sid-ok/select-account", {"index": 2})
    check(st == 200 and body == {"ok": True}, f"选中 index=2 -> 200 ok (st={st} body={body})")
    check(s.calls == [2], f"后端收到 index=2 (calls={s.calls})")

    st, body = await call("POST", "/api/accounts/login/sid-ok/select-account", {})
    check(st == 200 and s.calls[-1] == 0, f"缺省 index -> 0 (st={st} calls={s.calls})")

    s2 = FakeSess(ok=False)
    mgr.login_sessions["sid-bad"] = s2
    st, body = await call("POST", "/api/accounts/login/sid-bad/select-account", {"index": 1})
    check(st == 404, f"点击失败 -> 404 (st={st} body={body})")

    st, body = await call("POST", "/api/accounts/login/sid-none/select-account", {"index": 0})
    check(st == 404, f"未知 sid -> 404 (st={st} body={body})")

    st, body = await call("POST", "/api/accounts/login/sid-ok/select-account", {"index": "x"})
    check(st in (200, 422), f"非数字 index 不崩 (st={st} body={body})")

    print(f"\n=== 结果: {PASS} 通过 / {FAIL} 失败 ===")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
