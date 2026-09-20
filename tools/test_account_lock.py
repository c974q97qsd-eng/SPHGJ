"""测试账号微信身份锁定(不消耗扫码、不依赖 httpx、不写真实 config.json)。

分三部分:
  A. check_account_lock()  纯函数校验逻辑(锁定/未锁定/身份未知/名称相近等边界)
  B. set_account_lock()    真实实现,仅把 _save_config 换成计数桩,不落盘
  C. 路由                  lock / unlock / finalize 被拒,手搓 ASGI 直打 FastAPI app

运行: python tools/test_account_lock.py
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from backend import server  # noqa: E402
from backend.account_manager import AccountManager  # noqa: E402
from backend.login_capture import check_account_lock, LoginLockError  # noqa: E402


# ---------------------------------------------------------------- A. 纯函数
def test_check_lock():
    print("=== A. check_account_lock 校验逻辑 ===")
    locked = {"locked_finder_id": "FID_X", "locked_name": "示范店·甲仓"}

    # (captured, 期望通过?, 说明)
    cases = [
        ({"finder_id": "FID_X", "name": "任意显示名"}, True, "finder_id 相同 -> 放行"),
        ({"finder_id": "", "name": "示范店·甲仓"}, True, "选择页名精确相等 -> 放行"),
        ({"finder_id": "FID_Y", "name": "示范店·甲仓2号"}, False, "别的微信 -> 拒绝"),
        ({"finder_id": "", "name": ""}, False, "身份取不到 -> 拒绝(宁可不存)"),
        ({"finder_id": "FID_Y", "name": "示范店·甲仓2号"}, False, "名字相近但不等 -> 拒绝(防子串误判)"),
        ({"finder_id": "", "name": "示范店·甲"}, False, "前缀相同不等 -> 拒绝"),
    ]
    for captured, expect_pass, label in cases:
        reason = check_account_lock(locked, captured)
        check((not reason) == expect_pass, label, f"(reason={reason!r})")

    # 未锁定账号一律放行
    for captured in ({"finder_id": "FID_Y", "name": "别的号"}, {"finder_id": "", "name": ""}):
        check(not check_account_lock({}, captured), f"未锁定账号 -> 放行 (captured={captured})")

    # 只锁 finder_id(未锁 name):名字相同但 fid 不同 -> 拒绝(fid 是唯一标识)
    only_fid = {"locked_finder_id": "FID_X", "locked_name": ""}
    check(bool(check_account_lock(only_fid, {"finder_id": "FID_Y", "name": "示范店·甲仓"})),
          "只锁 fid 时,fid 不同即使名字相同 -> 拒绝")

    # 只锁 name:名字精确相等 -> 放行
    only_name = {"locked_finder_id": "", "locked_name": "示范店·甲仓"}
    check(not check_account_lock(only_name, {"finder_id": "FID_Y", "name": "示范店·甲仓"}),
          "只锁 name 时,名字相等 -> 放行")


# ------------------------------------------------- B. set_account_lock(真实实现)
class StubManager:
    """复用 AccountManager 的真实方法,仅把落盘换成计数,避免污染 config.json。"""

    workers = {}  # _account_status 会读,无 worker 即离线态

    def __init__(self, accounts):
        self.config = {"accounts": accounts}
        self.saved = 0

    def _save_config(self):
        self.saved += 1

    set_account_lock = AccountManager.set_account_lock
    _account_status = AccountManager._account_status


def test_set_lock():
    print("\n=== B. set_account_lock 真实实现(_save_config 打桩) ===")
    acc = {"id": "a1", "name": "示范店·乙仓", "_log_finder_id": "FID_A"}
    m = StubManager([acc])

    check(m.set_account_lock("a1", True) is True, "锁定成功 -> True")
    check(acc.get("locked_finder_id") == "FID_A" and acc.get("locked_name") == "示范店·乙仓",
          f"locked_* 已写入 (fid={acc.get('locked_finder_id')!r} name={acc.get('locked_name')!r})")
    check(m.saved == 1, f"落盘被调用 1 次 (saved={m.saved})")

    st = m._account_status(acc, {})
    check(st["locked"] is True and st["locked_name"] == "示范店·乙仓",
          f"status 输出 locked/locked_name ({st['locked']}, {st['locked_name']!r})")

    check(m.set_account_lock("a1", False) is True, "解锁成功 -> True")
    check("locked_finder_id" not in acc and "locked_name" not in acc, "解锁后 locked_* 已清除")
    check(m.saved == 2, f"落盘累计 2 次 (saved={m.saved})")
    st = m._account_status(acc, {})
    check(st["locked"] is False and st["locked_name"] == "", f"解锁后 status.locked=False ({st['locked']})")

    check(m.set_account_lock("__nope__", True) is False, "账号不存在 -> False")
    check(m.saved == 2, "失败时不落盘")

    # 无任何微信标识 -> 无法锁定
    m2 = StubManager([{"id": "a2", "name": ""}])
    check(m2.set_account_lock("a2", True) is False, "无微信标识 -> 拒绝锁定")


# ---------------------------------------------------------------- C. 路由
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


class RouteMgr:
    """桩 manager:只实现锁定路由用到的接口。"""

    def __init__(self, lock_result=True):
        self.lock_result = lock_result
        self.calls = []

    def set_account_lock(self, account_id, locked):
        self.calls.append((account_id, locked))
        return self.lock_result

    def status_snapshot(self):
        return {"accounts": [{"id": "a1", "name": "X", "locked": True, "locked_name": "X"}]}


async def test_routes():
    print("\n=== C. 路由 lock / unlock / finalize ===")

    mgr = RouteMgr(lock_result=True)
    server.manager = mgr

    st, body = await call("POST", "/api/accounts/a1/lock")
    check(st == 200 and body.get("locked") is True, f"lock -> 200 locked=true (st={st} body={body})")
    check(mgr.calls and mgr.calls[-1] == ("a1", True), f"后端收到 (a1, True) (calls={mgr.calls})")
    check(isinstance(body.get("accounts"), list), "lock 返回最新账号列表供前端直接刷新")

    st, body = await call("POST", "/api/accounts/a1/unlock")
    check(st == 200 and body.get("locked") is False, f"unlock -> 200 locked=false (st={st})")
    check(mgr.calls[-1] == ("a1", False), f"后端收到 (a1, False) (calls={mgr.calls})")

    server.manager = RouteMgr(lock_result=False)
    st, body = await call("POST", "/api/accounts/a1/lock")
    check(st == 400, f"无可锁定标识 -> 400 (st={st} detail={body.get('detail')!r})")
    st, body = await call("POST", "/api/accounts/a1/unlock")
    check(st == 404, f"解锁不存在的账号 -> 404 (st={st})")

    # finalize 被锁定拒绝 -> 403 且带中文原因
    msg = "该账号已锁定微信「示范店·甲仓」,本次登录的是「示范店·甲仓2号」,已拒绝保存"

    class FinalizeMgr:
        async def finalize_login(self, sid, account_id, name):
            raise LoginLockError(msg)

    server.manager = FinalizeMgr()
    st, body = await call("POST", "/api/accounts/login/sid-1/finalize", {})
    check(st == 403, f"锁定拒绝 -> 403 (st={st})")
    check(body.get("detail") == msg, f"403 带中文原因 (detail={body.get('detail')!r})")


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
    test_check_lock()
    test_set_lock()
    await test_routes()
    print(f"\n=== 结果: {PASS} 通过 / {FAIL} 失败 ===")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
