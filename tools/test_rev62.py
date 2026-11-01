# -*- coding: utf-8 -*-
"""rev62 单测:纯 HTTP 模式下写操作走 HTTP,不回退浏览器、不误触发退避。

不需要 playwright:api_client 只依赖 backend/wx_http(标准库/httpx),post_json 直接打桩。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api_client import WxApiClient
from backend import wx_http

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  << " + str(extra)) if not cond else ""))


class FakePage:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def evaluate(self, js, args=None):
        self.calls.append(args)
        return json.dumps(self.resp)


def mk(page, rc=None):
    cfg = {"accounts": [{"id": "a1", "_aid": "AID-1", "_log_finder_id": "FID-1"}],
           "risk_control": rc or {}}
    c = WxApiClient(page, cfg["accounts"][0], cfg, None)
    c._throttle = lambda path: asyncio.sleep(0)   # 跳过 4~8s 写间隔
    return c


def run(coro):
    return asyncio.run(coro)


def main():
    calls = []
    holder = {"resp": {"errCode": 0, "data": {"comment": {"commentId": "C-NEW"}}}}

    async def fake_post_json(acc, url, body, page_url="", headers=None, timeout=30):
        calls.append({"url": url, "body": body})
        return holder["resp"]

    wx_http.post_json = fake_post_json

    # ---------- 1. page=None 的写操作走 HTTP ----------
    calls.clear()
    holder["resp"] = {"errCode": 0, "data": {"comment": {"commentId": "C-NEW"}}}
    c = mk(None)
    r = run(c.post_comment("export/AAA", "测试内容"))
    check("1 page=None 写操作走 HTTP(请求真的发出去了)", len(calls) == 1, calls)
    check("1 返回成功且带 exportId",
          r.get("errCode") == 0 and calls and calls[0]["body"].get("exportId") == "export/AAA", r)
    check("1 不判失败、不计退避", c._consecutive_failures == 0 and c.failed(r) is False)
    check("1 记一次写计数", c._daily_count(__import__("time").strftime("%Y-%m-%d")) == 1)
    check("1 打了「HTTP 写操作可用」标记", c._http_logged is True)

    # ---------- 2. 无 cookie:如实报 no_page:no_cookie,且不退避 ----------
    calls.clear()
    holder["resp"] = {"__err": "no_cookie"}
    c = mk(None)
    r = run(c.post_comment("export/AAA", "x"))
    check("2 no_cookie 时 err 如实带上原因",
          isinstance(r, dict) and str(r.get("__err")) == "no_page:no_cookie", r)
    check("2 no_cookie 不触发退避(原来会 30/120/600s 越退越久)",
          c._consecutive_failures == 0 and c._backoff_until == 0.0)
    check("2 上层仍判失败(不会假成功)", c.failed(r) is True)

    # ---------- 3. 真业务失败(300800)仍要计数退避 ----------
    calls.clear()
    holder["resp"] = {"errCode": 300800, "errMsg": "request failed"}
    c = mk(None)
    r = run(c.post_comment("export/AAA", "x"))
    check("3 errCode=300800 判失败(HTTP 侧换成 http_errCode)",
          c.failed(r) is True and "300800" in str(r.get("__err")), r)
    check("3 仍然计一次失败(真接口错误要退避)", c._consecutive_failures == 1)

    # ---------- 4. 有页面时写操作仍走页面(行为不变) ----------
    calls.clear()
    pg = FakePage({"errCode": 0, "data": {"comment": {"commentId": "C-PAGE"}}})
    c = mk(pg)
    r = run(c.post_comment("export/BBB", "内容"))
    check("4 有 page 时写操作走页面 evaluate", len(pg.calls) == 1, pg.calls)
    check("4 没有多余的 HTTP 请求", len(calls) == 0, calls)
    check("4 结果正常", r.get("errCode") == 0)

    # ---------- 5. 灰度名单仍然生效 ----------
    calls.clear()
    pg = FakePage({"errCode": 0})
    c = mk(pg, {"api_write_http_accounts": ["a1"]})
    run(c.post_comment("export/CCC", "内容"))
    check("5 灰度名单命中 -> 强制走 HTTP", len(calls) == 1 and len(pg.calls) == 0)

    # ---------- 6. 读路径没被动到 ----------
    calls.clear()
    holder["resp"] = {"errCode": 0, "data": {"list": []}}
    pg = FakePage({"errCode": 0})
    c = mk(pg)
    run(c.fetch_video_list())
    check("6 page 存在时读仍走 HTTP(原始设计)", len(calls) == 1 and len(pg.calls) == 0)

    print()
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
