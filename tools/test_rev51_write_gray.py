# -*- coding: utf-8 -*-
r"""
rev51c 单元测试：写操作灰度 + 写操作失败不重试

重点验证两件事：
  1. 灰度名单能精确控制「哪个账号的写操作走 HTTP」
  2. 写操作 HTTP 失败时**不能**回退浏览器重发（否则重复评论）
     唯一例外是 no_cookie —— 那时请求根本没发出去
"""
import asyncio
import sys

sys.path.insert(0, r"D:\SPHGJ")

from backend import api_client as ac      # noqa: E402
from backend import wx_http               # noqa: E402

results = []


def chk(ok, msg):
    results.append((ok, msg))
    print(("  PASS  " if ok else "  FAIL  ") + msg)


def make(account=None, rc=None):
    cfg = {"risk_control": rc or {}, "accounts": [account] if account else []}
    return ac.WxApiClient(page=None, account=account or {}, config=cfg)


ACC = {"id": "a1", "_log_finder_id": "uspoloassn1", "_aid": "123"}


def main():
    print("[1] 默认配置：读 HTTP / 写 浏览器")
    c = make(ACC, {})
    chk(c._via_http(False) is True, "读操作默认走 HTTP")
    chk(c._via_http(True) is False, "写操作默认走浏览器")

    print("[2] 灰度名单")
    c = make(ACC, {"api_write_http_accounts": ["uspoloassn1"]})
    chk(c._via_http(True) is True, "短名命中 -> 写操作走 HTTP")
    chk(c._via_http(False) is True, "读操作不受影响")

    c = make(ACC, {"api_write_http_accounts": ["a1"]})
    chk(c._via_http(True) is True, "account id 命中 -> 写走 HTTP")

    c = make(ACC, {"api_write_http_accounts": ["other"]})
    chk(c._via_http(True) is False, "未命中 -> 写仍走浏览器")

    c = make(ACC, {"api_write_http_accounts": "uspoloassn1,a1"})
    chk(c._via_http(True) is True, "字符串逗号分隔也能解析")
    chk(c._write_http_accounts == {"uspoloassn1", "a1"}, "逗号分隔解析结果正确")

    print("[3] 全局开关优先")
    c = make(ACC, {"api_write_via": "http"})
    chk(c._via_http(True) is True, "api_write_via=http -> 所有账号写走 HTTP")

    print("[4] cookie 失效阻塞期内一律走浏览器")
    c = make(ACC, {"api_write_via": "http"})
    c._http_block_until = 9e18
    chk(c._via_http(True) is False, "阻塞期内写操作走浏览器")
    chk(c._via_http(False) is False, "阻塞期内读操作也走浏览器")

    print("[5] 写操作失败语义（核心安全线）")
    c = make(ACC, {})

    # 5.1 no_cookie -> 请求没发出去,可以安全回退
    async def fake_nocookie(*a, **kw):
        return {"__err": "no_cookie"}
    orig = wx_http.post_json
    wx_http.post_json = fake_nocookie
    try:
        r = asyncio.run(c._post_via_http("comment/create_comment", "u", {}, "p", is_write=True))
        chk(r is None, "写操作 no_cookie -> 回退浏览器（安全）")

        # 5.2 连接异常 -> 服务端可能已处理,绝不能回退
        async def fake_exc(*a, **kw):
            raise RuntimeError("connection reset")
        wx_http.post_json = fake_exc
        r = asyncio.run(c._post_via_http("comment/create_comment", "u", {}, "p", is_write=True))
        chk(isinstance(r, dict) and r.get("__no_retry") is True,
            "写操作连接异常 -> 不回退，标记 __no_retry")

        # 5.3 errCode 非 0 -> 服务端明确拒绝,不回退
        async def fake_code(*a, **kw):
            return {"errCode": 300333}
        wx_http.post_json = fake_code
        r = asyncio.run(c._post_via_http("comment/create_comment", "u", {}, "p", is_write=True))
        chk(isinstance(r, dict) and r.get("__no_retry") is True,
            "写操作 errCode=300333 -> 不回退")

        # 5.4 读操作同样异常 -> 必须回退（幂等）
        c2 = make(ACC, {})
        r = asyncio.run(c2._post_via_http("comment/comment_list", "u", {}, "p", is_write=False))
        chk(r is None, "读操作连接异常 -> 仍回退浏览器（幂等）")

        # 5.5 读操作 errCode -> 回退
        c3 = make(ACC, {})
        r = asyncio.run(c3._post_via_http("comment/comment_list", "u", {}, "p", is_write=False))
        chk(r is None, "读操作 errCode=300333 -> 回退浏览器")

        # 5.6 写操作成功 -> 原样返回,不带 __no_retry
        async def fake_ok(*a, **kw):
            return {"errCode": 0, "data": {"commentId": "x"}}
        wx_http.post_json = fake_ok
        c4 = make(ACC, {})
        r = asyncio.run(c4._post_via_http("comment/create_comment", "u", {}, "p", is_write=True))
        chk(isinstance(r, dict) and r.get("errCode") == 0 and "__no_retry" not in r,
            "写操作成功 -> 正常返回")
    finally:
        wx_http.post_json = orig

    print("[6] 300333 会触发 180s 阻塞")
    c5 = make(ACC, {})

    async def fake_333(*a, **kw):
        return {"errCode": 300333}
    wx_http.post_json = fake_333
    try:
        before = c5._http_block_until
        asyncio.run(c5._post_via_http("post/post_list", "u", {}, "p", is_write=False))
        chk(c5._http_block_until > before, "读操作 300333 -> 触发阻塞")
    finally:
        wx_http.post_json = orig

    ok = all(r[0] for r in results)
    n_ok = sum(1 for r in results if r[0])
    print("\nRESULT:", ("ALL PASS (%d/%d)" % (n_ok, len(results))) if ok
          else ("HAS FAILURE (%d/%d)" % (n_ok, len(results))))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
