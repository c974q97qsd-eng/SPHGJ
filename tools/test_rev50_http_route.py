# -*- coding: utf-8 -*-
"""rev50 单元测试: HTTP 直连路由 + 回退链。

核心不变量:
  - 读默认走 http，写默认走 browser
  - HTTP 任何异常(no_cookie / errCode!=0 / 网络错) 必须回退浏览器，且不污染 _post 的返回值
  - no_cookie 后 180s 内不再尝试 HTTP（避免每轮白跑）
  - URL 里的 _aid 按 config 最新值纠正（重扫后 _aid 会变）

跑法: python tools/test_rev50_http_route.py
"""
import asyncio
import sys
import types

sys.path.insert(0, r"D:\SPHGJ")


def _stub_playwright():
    class _Any:
        def __getattr__(self, k):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    for name in ("playwright", "playwright.async_api", "playwright.sync_api",
                 "playwright._impl", "playwright._impl._errors"):
        m = types.ModuleType(name)
        m.__path__ = []
        m.__getattr__ = lambda k: _Any()
        sys.modules[name] = m


_stub_playwright()

import backend.wx_http as wx                       # noqa: E402
import backend.api_client as api_mod               # noqa: E402
from backend.api_client import WxApiClient         # noqa: E402
from backend.post_fetcher import PostFetcher       # noqa: E402

PASS = []


def ok(msg):
    PASS.append(msg)
    print("PASS  " + msg)


class FakeStorage:
    def __init__(self):
        self.n = 0

    def get_daily_write_count(self, aid, day):
        return self.n

    def incr_daily_write_count(self, aid, day):
        self.n += 1


def make_client(config=None, account=None, storage=None):
    acc = account or {"id": "a1", "_aid": "AID_OLD", "_log_finder_id": "FID1"}
    cfg = config if config is not None else {"accounts": [acc]}
    cli = WxApiClient(page=None, account=acc, config=cfg, storage=storage or FakeStorage())
    cli._read_interval = (0, 0)      # 关掉防抖节流，测试才能跑快
    cli._write_interval = (0, 0)
    cli._last_req_time = 0.0
    cli.page_calls = []

    async def fake_page(path, url, body):
        cli.page_calls.append(path)
        return {"ret": 0, "from": "page"}

    cli._post_via_page = fake_page
    return cli


def patch_post_json(fn):
    orig = wx.post_json
    api_mod.wx_http.post_json = fn
    return orig


async def t_read_via_http():
    cli = make_client()
    seen = {}

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        seen["url"] = url
        return {"ret": 0, "from": "http"}

    orig = patch_post_json(fake)
    try:
        r = await cli._post("comment/comment_list", {"x": 1})
    finally:
        api_mod.wx_http.post_json = orig
    assert r["from"] == "http", r
    assert cli.page_calls == [], cli.page_calls
    assert "/comment/comment_list" in seen["url"]
    ok("读接口默认走 HTTP，不碰浏览器")


async def t_write_via_browser():
    cli = make_client()
    called = {"http": 0}

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        called["http"] += 1
        return {"ret": 0}

    orig = patch_post_json(fake)
    try:
        r = await cli._post("comment/create_comment", {"x": 1})
    finally:
        api_mod.wx_http.post_json = orig
    assert called["http"] == 0, "写操作不该走 HTTP"
    assert r["from"] == "page", r
    ok("写接口默认走浏览器（风控未验证，留退路）")


async def t_write_via_http_when_configured():
    cli = make_client(config={"accounts": [{"id": "a1", "_aid": "AID"}],
                              "risk_control": {"api_write_via": "http"}})

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        return {"ret": 0, "from": "http"}

    orig = patch_post_json(fake)
    try:
        r = await cli._post("comment/create_comment", {"x": 1})
    finally:
        api_mod.wx_http.post_json = orig
    assert r["from"] == "http", r
    assert cli.page_calls == []
    ok("api_write_via=http 时写接口也走 HTTP")


async def t_no_cookie_fallback_and_block():
    cli = make_client()
    called = {"http": 0}

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        called["http"] += 1
        return {"__err": "no_cookie"}

    orig = patch_post_json(fake)
    try:
        r1 = await cli._post("comment/comment_list", {})
        assert r1["from"] == "page", r1
        assert called["http"] == 1
        # 第二次：180s 内不该再试 HTTP
        r2 = await cli._post("comment/comment_list", {})
        assert r2["from"] == "page", r2
        assert called["http"] == 1, "no_cookie 后应暂停 180s，实际又试了 %d 次" % called["http"]
    finally:
        api_mod.wx_http.post_json = orig
    assert len(cli.page_calls) == 2
    ok("no_cookie -> 回退浏览器，且 180s 内不再尝试 HTTP")


async def t_errcode_fallback():
    for code, block_expected in ((300333, True), (100001, False)):
        cli = make_client()

        async def fake(acc, url, body, page_url="", headers=None, timeout=20, _c=code):
            return {"errCode": _c}

        orig = patch_post_json(fake)
        try:
            r = await cli._post("comment/comment_list", {})
        finally:
            api_mod.wx_http.post_json = orig
        assert r["from"] == "page", (code, r)
        blocked = cli._http_block_until > 0
        assert blocked == block_expected, (code, blocked)
    ok("errCode!=0 一律回退浏览器；300333 额外暂停 180s")


async def t_network_error_fallback():
    cli = make_client()

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        return {"__err": "request_error"}

    orig = patch_post_json(fake)
    try:
        r = await cli._post("comment/comment_list", {})
    finally:
        api_mod.wx_http.post_json = orig
    assert r["from"] == "page", r
    ok("网络错 -> 回退浏览器")


async def t_aid_refresh():
    stale = {"id": "a1", "_aid": "AID_STALE"}
    fresh = {"id": "a1", "_aid": "AID_FRESH"}
    cli = WxApiClient(page=None, account=stale, config={"accounts": [fresh]},
                      storage=FakeStorage())
    cli._read_interval = (0, 0)
    cli._last_req_time = 0.0
    seen = {}

    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        seen["url"] = url
        return {"ret": 0}

    orig = patch_post_json(fake)
    try:
        await cli._post("comment/comment_list", {})
    finally:
        api_mod.wx_http.post_json = orig
    assert "AID_FRESH" in seen["url"], seen["url"]
    assert "AID_STALE" not in seen["url"]
    ok("URL 的 _aid 自动纠正为 config 最新值")


async def t_post_fetcher_http():
    acc = {"id": "a1", "_aid": "AID", "_log_finder_id": "FID1"}
    pf = PostFetcher(storage=None, account=acc, config={"accounts": [acc]})

    async def fake(acc_, url, body, page_url="", headers=None, timeout=20):
        assert body["userpageType"] == 11
        assert body["_log_finder_id"] == "FID1"
        return {"errCode": 0, "data": {"list": [{"objectId": "o1"}]}}

    orig = patch_post_json(fake)
    try:
        j = await pf._post_list_http(1)
        assert j and j["data"]["list"], j
        # 再测不可用 -> None（调用方据此回退浏览器）
        async def bad(acc_, url, body, page_url="", headers=None, timeout=20):
            return {"__err": "no_cookie"}
        api_mod.wx_http.post_json = bad
        assert await pf._post_list_http(1) is None
        # errCode 非 0 同样视为不可用
        async def bad2(acc_, url, body, page_url="", headers=None, timeout=20):
            return {"errCode": 300333}
        api_mod.wx_http.post_json = bad2
        assert await pf._post_list_http(1) is None
    finally:
        api_mod.wx_http.post_json = orig
    ok("post_fetcher 取页：成功给数据，失败给 None（触发浏览器回退）")


def t_wx_http_primitives():
    ss = {"cookies": [
        {"name": "sessionid", "value": "S1", "domain": ".weixin.qq.com"},
        {"name": "wxuin", "value": "U1", "domain": "channels.weixin.qq.com"},
        {"name": "bid", "value": "BAD", "domain": "developers.weixin.qq.com"},
    ]}
    h = wx.build_cookie_header(ss)
    assert "sessionid=S1" in h and "wxuin=U1" in h
    assert "developers" not in h and "bid=BAD" not in h
    b = wx.std_body("FID_X", pageSize=200, currentPage=3)
    assert b["_log_finder_id"] == "FID_X" and b["pageSize"] == 200
    assert b["timestamp"].isdigit() and len(b["timestamp"]) == 13
    u = wx.api_url("https://a.com/micro", "post/post_list", "AID", "https://a.com/x")
    assert "_aid=AID" in u and "_pageUrl=https%3A%2F%2Fa.com%2Fx" in u
    ok("wx_http 原语：cookie 域过滤 / std_body / api_url 编码")


async def main():
    t_wx_http_primitives()
    await t_read_via_http()
    await t_write_via_browser()
    await t_write_via_http_when_configured()
    await t_no_cookie_fallback_and_block()
    await t_errcode_fallback()
    await t_network_error_fallback()
    await t_aid_refresh()
    await t_post_fetcher_http()
    print("\nALL PASS (%d/%d)" % (len(PASS), len(PASS)))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
