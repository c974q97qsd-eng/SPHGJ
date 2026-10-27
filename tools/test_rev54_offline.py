# -*- coding: utf-8 -*-
"""rev54 单测:账号在网状态机(掉线不再发包)。

用户 2026-10-28 的规则:
    「只要你掉线状态是准确的,掉线状态的账号就不用去发包了,
      直到扫码登录后才加入轮询任务。」

跑法:python tools/test_rev54_offline.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 本地没装 playwright(只装在母本),stub 掉只为能 import account_manager 链路
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

from backend import wx_http
from backend.api_client import WxApiClient
from backend.account_manager import AccountWorker, AccountManager, auth_fingerprint

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append(name + (" | " + str(extra) if extra else ""))
        print(f"  FAIL  {name}  {extra}")


COOKIE_ACC = {
    "id": "t1",
    "profile_dir": os.path.join(tempfile.gettempdir(), "rev54_profile"),
    "_aid": "123",
    "_log_finder_id": "finderX",
    "storage_state": {"cookies": [
        {"name": "sessionid", "value": "AAA", "domain": ".weixin.qq.com", "path": "/"},
        {"name": "wxuin", "value": "999", "domain": ".weixin.qq.com", "path": "/"},
    ]},
}
NO_COOKIE_ACC = {"id": "t2", "profile_dir": COOKIE_ACC["profile_dir"], "storage_state": {}}


def make_worker(acc=None, emit=None):
    ev = []

    async def _emit(event, payload):
        ev.append((event, payload))

    async def _noop(*a, **k):
        return None

    w = AccountWorker(dict(acc or COOKIE_ACC), None, {}, emit or _emit)
    w._events = ev
    w.on_auth_change = _noop
    return w


class FakeFetcher:
    def __init__(self, res=(1, 2, [], [], True)):
        self.calls = 0
        self.res = res

    async def fetch_all(self, on_batch=None):
        self.calls += 1
        return self.res


# ==================== 1. 登录态指纹 ====================
def t_fingerprint():
    fp = auth_fingerprint(COOKIE_ACC)
    check("指纹:有 cookie 得到非空 md5", len(fp) == 32, fp)
    check("指纹:无 cookie 为空串", auth_fingerprint(NO_COOKIE_ACC) == "")
    check("指纹:同 cookie 稳定", auth_fingerprint(dict(COOKIE_ACC)) == fp)
    other = {"storage_state": {"cookies": [
        {"name": "sessionid", "value": "BBB", "domain": ".weixin.qq.com", "path": "/"}]}}
    check("指纹:cookie 变了指纹就变", auth_fingerprint(other) != fp)


# ==================== 2. api_client 标记登录态失效 ====================
async def t_api_auth_fail():
    orig = wx_http.post_json

    async def fake_300333(acc, url, body, page_url=None, timeout=30):
        return {"errCode": 300333, "errMsg": "sessionid invalid"}

    async def fake_ok(acc, url, body, page_url=None, timeout=30):
        return {"errCode": 0, "data": {}}

    try:
        wx_http.post_json = fake_300333
        c = WxApiClient(None, dict(COOKIE_ACC), {}, None)
        c._read_interval = (0.0, 0.0)
        check("api:初始 http_available=True", c.http_available() is True)
        check("api:初始 auth_fail 为空", c.auth_fail is None)
        await c._post("post/post_list", c._body(pageSize=1))
        check("api:300333 置位 auth_fail", (c.auth_fail or {}).get("code") == 300333, c.auth_fail)
        check("api:置位后 http_available=False", c.http_available() is False)
        check("api:置位后 _via_http 读操作=False", c._via_http(False) is False)

        # clear_auth_fail 之后能恢复,成功响应也会清标记
        c.clear_auth_fail()
        check("api:clear_auth_fail 清空", c.auth_fail is None)
        check("api:清空后 http_available 恢复", c.http_available() is True)
        wx_http.post_json = fake_ok
        await c._post("post/post_list", c._body(pageSize=1))
        check("api:成功响应后 auth_fail 仍为空", c.auth_fail is None)

        # 300330 同样认
        wx_http.post_json = fake_300333
        c2 = WxApiClient(None, dict(COOKIE_ACC), {}, None)
        c2._read_interval = (0.0, 0.0)
        c2.auth_fail = {"code": 300330, "ts": time.time(), "path": "x"}
        check("api:300330 也阻塞 http_available", c2.http_available() is False)
    finally:
        wx_http.post_json = orig


# ==================== 3. mark_offline / mark_online ====================
async def t_mark_offline():
    w = make_worker()
    changes = []

    async def cb(aid, state, reason):
        changes.append((aid, state, reason))

    w.on_auth_change = cb
    w.logged_in = True
    w._browser_refs = 3
    await w.mark_offline("测试掉线", code=300333)
    check("offline:状态置 offline", w.auth_state == "offline")
    check("offline:reason/code 记录", w.auth_reason == "测试掉线" and w.auth_code == 300333)
    check("offline:logged_in 归零", w.logged_in is False)
    check("offline:在线事件被清除", w._online_event.is_set() is False)
    check("offline:引用计数清零(防泄漏)", w._browser_refs == 0)
    check("offline:发出 account_offline 事件",
          any(e[0] == "account_offline" for e in w._events), [e[0] for e in w._events])
    check("offline:回调收到 offline", changes and changes[-1][1] == "offline", changes)

    # 幂等:重复调用不重复触发回调
    n = len(changes)
    await w.mark_offline("另一条原因")
    check("offline:重复调用不重复回调", len(changes) == n)
    check("offline:重复调用更新 reason", w.auth_reason == "另一条原因")

    # 恢复
    changes.clear()
    await w.mark_online("单测恢复")
    check("online:状态回 ok", w.auth_state == "ok")
    check("online:reason 清空", w.auth_reason == "")
    check("online:在线事件置位", w._online_event.is_set() is True)
    check("online:回调收到 ok", changes and changes[-1][1] == "ok", changes)


# ==================== 4. 离线账号一个包都不发 ====================
async def t_no_request_when_offline():
    w = make_worker()
    w.fetcher = FakeFetcher()

    class BoomApi:
        def http_available(self):
            raise AssertionError("离线账号不该问 http_available")

    w.api = BoomApi()
    w.auth_state = "offline"
    res = await w._run_fetch_round()
    check("离线:抓取轮直接返回 (None, False)", res == (None, False), res)
    check("离线:fetcher 一次都没被调用", w.fetcher.calls == 0)

    # 直播轻量探测也不走
    called = []
    async def fake_probe():
        called.append(1)
        return {"live": False}
    w._probe_via_http = fake_probe
    check("离线:轻量探测返回 None", await w._probe_light() is None)
    check("离线:探测一次 HTTP 都没发", called == [])

    # 手动「立即抓取」同样不发
    r = await w.fetch_once()
    check("离线:fetch_once 返回空结果", r == (0, 0, [], [], False), r)
    check("离线:fetch_once 也没调 fetcher", w.fetcher.calls == 0)


# ==================== 5. 判离线的证据必须确定 ====================
async def t_offline_evidence():
    # 开浏览器失败/硬验无结论 -> 不判离线,但必须释放引用
    w = make_worker()
    w.fetcher = FakeFetcher()
    w.api = None
    w._verify_result = None

    async def fake_ensure_fail():
        return False

    w.ensure_browser = fake_ensure_fail
    released = []

    async def fake_release():
        released.append(1)

    w.release_idle_browser = fake_release
    w._run_fetch_round_lock_release = True
    res = await w._run_fetch_round()
    check("证据:无结论时不判离线", w.auth_state != "offline", w.auth_state)
    check("证据:但仍返回 used_browser=True 让上层 release", res == (None, True), res)
    check("证据:fetcher 未被调用", w.fetcher.calls == 0)

    # 硬验明确 no -> 判离线
    w2 = make_worker()
    w2.fetcher = FakeFetcher()
    w2.api = None
    w2._verify_result = "no"
    w2.ensure_browser = fake_ensure_fail
    res2 = await w2._run_fetch_round()
    check("证据:硬验 no 时判离线", w2.auth_state == "offline", w2.auth_state)
    check("证据:reason 写明双路都无登录态", "未登录" in w2.auth_reason, w2.auth_reason)
    check("证据:判离线后仍返回 used_browser=True", res2 == (None, True), res2)


# ==================== 6. _wait_online ====================
async def t_wait_online():
    w = make_worker()
    t0 = time.time()
    await w._wait_online(1.0)
    check("等待:非离线时立即返回", time.time() - t0 < 0.2)

    w.auth_state = "offline"
    w._online_event.clear()
    t0 = time.time()
    await w._wait_online(0.25)
    el = time.time() - t0
    check("等待:离线时等到超时才返回", el >= 0.24, el)

    w._online_event.set()
    t0 = time.time()
    await w._wait_online(3.0)
    check("等待:置位后立即返回", time.time() - t0 < 0.2)


# ==================== 7. 跨重启的离线记录 ====================
async def t_persist():
    m = AccountManager({"accounts": []}, None, None)
    m._auth_file = os.path.join(tempfile.gettempdir(), "rev54_auth_state_test.json")
    try:
        os.remove(m._auth_file)
    except FileNotFoundError:
        pass
    m._auth_offline = {}

    check("持久化:无记录时不认为是认识离线", m._is_known_offline(COOKIE_ACC) is False)
    m._auth_offline["t1"] = {"reason": "300333", "ts": 1.0, "fp": auth_fingerprint(COOKIE_ACC)}
    check("持久化:指纹一致 -> 认识离线", m._is_known_offline(COOKIE_ACC) is True)
    changed = dict(COOKIE_ACC)
    changed["storage_state"] = {"cookies": [
        {"name": "sessionid", "value": "NEW", "domain": ".weixin.qq.com", "path": "/"}]}
    check("持久化:登录态变了 -> 不再是认识离线(说明重扫过)",
          m._is_known_offline(changed) is False)

    m._save_auth_offline()
    check("持久化:落盘文件存在", os.path.exists(m._auth_file))
    m2 = AccountManager({"accounts": []}, None, None)
    m2._auth_file = m._auth_file
    m2._auth_offline = m2._load_auth_offline()
    check("持久化:重新载入内容一致", (m2._auth_offline.get("t1") or {}).get("reason") == "300333",
          m2._auth_offline)
    m2._clear_auth_offline("t1")
    check("持久化:扫码成功后清记录", m2._auth_offline == {})

    # worker 状态变化 -> manager 落盘
    w = make_worker()
    w.on_auth_change = m2._on_worker_auth
    m2.workers = {"t1": w}
    m2.config["accounts"] = [dict(COOKIE_ACC)]
    m2._relogin_queue = []
    m2._relogin_active_acc = None
    await w.mark_offline("300333", code=300333)
    await asyncio.sleep(0.05)   # 排队扫码是 create_task
    check("回调:掉线后 manager 落盘",
          (m2._auth_offline.get("t1") or {}).get("fp") == auth_fingerprint(COOKIE_ACC),
          m2._auth_offline)
    await w.mark_online("恢复")
    await asyncio.sleep(0.05)
    check("回调:恢复后 manager 清记录", "t1" not in m2._auth_offline, m2._auth_offline)


# ==================== 8. 状态快照 / 立即抓取过滤 ====================
async def t_status_and_batch():
    m = AccountManager({"accounts": []}, None, None)
    w = make_worker()
    w.auth_state = "offline"
    w.auth_reason = "300333"
    w.auth_ts = 123.0
    w.logged_in = True          # 故意留着旧值,验证 auth_state 优先
    m.workers = {"t1": w}
    st = m._account_status({"id": "t1", "name": "测试号"}, {})
    check("状态:快照带 auth_state", st.get("auth_state") == "offline", st.get("auth_state"))
    check("状态:快照带 auth_reason", st.get("auth_reason") == "300333")
    check("状态:快照带离线时间", st.get("auth_offline_at") == 123.0)

    w.fetcher = FakeFetcher()
    w.api = None
    r = await m.fetch_all_once()
    check("批量:离线账号不进立即抓取", w.fetcher.calls == 0 and list(r) == [], r)


# ==================== 9. 关 context 后 api 不能还握着死 page(rev54c) ====================
async def t_close_context_api():
    w = make_worker()
    w.api = WxApiClient(None, dict(COOKIE_ACC), {}, None)
    w.api.auth_fail = {"code": 300333, "ts": 1.0, "path": "post/post_list"}
    w.api._http_block_until = time.time() + 170
    w.api._http_logged = True
    old_api = w.api
    w.fetcher = FakeFetcher()
    w.fetcher.api = old_api
    await w._close_context()
    check("关浏览器:换成新客户端", w.api is not old_api)
    check("关浏览器:新客户端没有 page", getattr(w.api, "page", "x") is None)
    check("关浏览器:auth_fail 被继承", (w.api.auth_fail or {}).get("code") == 300333)
    check("关浏览器:180s 退避被继承", w.api._http_block_until > time.time())
    check("关浏览器:fetcher 重绑到新客户端", w.fetcher.api is w.api)


async def main():
    t_fingerprint()
    await t_api_auth_fail()
    await t_mark_offline()
    await t_no_request_when_offline()
    await t_offline_evidence()
    await t_wait_online()
    await t_persist()
    await t_status_and_batch()
    await t_close_context_api()


if __name__ == "__main__":
    asyncio.run(main())
    print()
    print(f"共 {PASS + FAIL} 项:PASS {PASS} / FAIL {FAIL}")
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print("  -", f)
    sys.exit(1 if FAIL else 0)
