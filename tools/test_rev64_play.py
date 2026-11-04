# -*- coding: utf-8 -*-
"""rev64 单测：纯 HTTP 现取播放地址，不依赖 worker / 浏览器。

本机没装 playwright 也能跑 —— 只 import backend.post_fetcher(依赖全是标准库)，
wx_http.post_json / cookie_header_of 用 fake 顶掉，绝不出网。
"""
import ast
import asyncio
import io
import os
import sys

ROOT = r"D:\SPHGJ"
sys.path.insert(0, ROOT)
from backend import post_fetcher as pfmod  # noqa: E402

FID = "finder_test@finder"
OID = "export/TEST_OBJECT_ID"
FRESH = ("https://finder.video.qq.com/251/20302/stodownload?encfilekey="
         + "A" * 700 + "&token=xyz&sign=END")
RESULT = []
ORIG_POST = pfmod.wx_http.post_json
ORIG_COOKIE = pfmod.wx_http.cookie_header_of


def check(name, cond, extra=""):
    RESULT.append((name, bool(cond), extra))
    print("  %s %s%s" % ("OK " if cond else "BAD", name,
                         "" if cond else "  <- " + str(extra)[:160]))


def account(with_cookie=True, with_aid=True):
    return {
        "id": "test_account",
        "_log_finder_id": FID,
        "_aid": "aid-123456" if with_aid else "",
        "storage_state": {"cookies": [{"name": "sessionid", "value": "x",
                                       "domain": "channels.weixin.qq.com"}]}
        if with_cookie else {},
    }


def storage():
    class S:
        db_path = r"D:\SPHGJ\data\test.db"
    return S()


def fetcher(acc=None, cfg=None):
    return pfmod.PostFetcher(storage(), acc or account(), cfg or {"accounts": [acc or account()]})


def page(pg, hit=False, n=200, total=2582, err_code=0):
    lst = []
    for i in range(n):
        oid = OID if (hit and i == 0) else "export/OTHER_%d_%d" % (pg, i)
        lst.append({"objectId": oid, "exportId": oid,
                    "desc": {"media": [{"url": FRESH if oid == OID else "https://cdn/old"}]}})
    return {"errCode": err_code,
            "data": {"list": lst, "total": total}}


def install(pages, calls=None):
    """pages: {页码: 返回dict} 或 callable(pg)->dict。"""
    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        pg = int(body.get("currentPage") or 1)
        if calls is not None:
            calls.append(pg)
        if callable(pages):
            return pages(pg)
        if pg not in pages:
            return {"errCode": 0, "data": {"list": [], "total": 0}}
        return pages[pg]
    pfmod.wx_http.post_json = fake
    pfmod.wx_http.cookie_header_of = (
        lambda acc: "sessionid=x" if ((acc or {}).get("storage_state") or {}).get("cookies") else "")


def restore():
    pfmod.wx_http.post_json = ORIG_POST
    pfmod.wx_http.cookie_header_of = ORIG_COOKIE


class Ctx:
    def __init__(self):
        self.new_pages = 0

    async def new_page(self):
        self.new_pages += 1
        raise AssertionError("HTTP 已成功，不应再开浏览器页面")


class Worker:
    def __init__(self, context=None):
        self.context = context
        self.logged_in = True
        self.account = account()


async def run():
    # 1) 无 worker / 无 context 也应取到（本次故障的核心）
    calls = []
    install({1: page(1, hit=True)}, calls)
    f = fetcher()
    url, why = await f.find_video_url_http(OID)
    check("1 无 context / 无 worker 也能 HTTP 现取", url == FRESH and why == "", (url[:60], why))
    check("2 只发 1 次请求", calls == [1], calls)
    check("3 返回完整 URL 未截断", len(url) == len(FRESH) and url.endswith("&sign=END"), len(url))
    restore()

    # 4) 老作品在第 13 页，翻到命中
    calls = []
    pages = {p: page(p) for p in range(1, 13)}
    pages[13] = page(13, hit=True, n=182)
    install(pages, calls)
    f = fetcher()
    url, why = await f.find_video_url_http(OID)
    check("4 老作品第 13 页命中", url == FRESH and why == "", (url[:50], why))
    check("5 翻页顺序 1..13", calls == list(range(1, 14)), calls)
    restore()

    # 6) total 到头仍没找到 -> not_in_list，不会无限翻
    calls = []
    install(lambda pg: page(pg, n=200 if pg < 13 else 82, total=2582), calls)
    f = fetcher()
    url, why = await f.find_video_url_http(OID)
    check("6 列表翻完没找到", url == "" and why == "not_in_list", why)
    check("7 total 早停 13 页", len(calls) == 13, calls)
    restore()

    # 8) 空列表
    install({1: {"errCode": 0, "data": {"list": [], "total": 0}}})
    url, why = await fetcher().find_video_url_http(OID)
    check("8 首页空列表", url == "" and why == "not_in_list", why)
    restore()

    # 9) 无 cookie / 无 aid：不发包
    calls = []
    install({1: page(1, hit=True)}, calls)
    url, why = await fetcher(acc=account(with_cookie=False)).find_video_url_http(OID)
    check("9 无 cookie 报准确原因且不发包", url == "" and why == "no_cookie" and calls == [], (why, calls))
    url, why = await fetcher(acc=account(with_aid=False)).find_video_url_http(OID)
    check("10 无 _aid 报准确原因且不发包", url == "" and why == "no_aid" and calls == [], (why, calls))
    restore()

    # 11) 服务端错误码 / 传输错误
    install({1: page(1, err_code=300333)})
    url, why = await fetcher().find_video_url_http(OID)
    check("11 errCode 300333 原样上报", why == "errCode:300333", why)
    restore()
    install({1: {"__err": "no_cookie"}})
    url, why = await fetcher().find_video_url_http(OID)
    check("12 __err 原样上报", why == "http:no_cookie", why)
    restore()
    async def boom(*a, **kw):
        raise RuntimeError("network down")
    pfmod.wx_http.post_json = boom
    pfmod.wx_http.cookie_header_of = lambda acc: "sessionid=x"
    url, why = await fetcher().find_video_url_http(OID)
    check("13 请求异常原样上报", why == "request_error:RuntimeError", why)
    restore()

    # 14) max_pages 上限
    calls = []
    install(lambda pg: page(pg), calls)
    url, why = await fetcher().find_video_url_http(OID, max_pages=2)
    check("14 翻页上限", url == "" and why == "page_limit" and calls == [1, 2], (why, calls))
    restore()

    # 15) find_video_url：HTTP 成功时不碰浏览器
    install({1: page(1, hit=True)})
    ctx = Ctx()
    f = fetcher()
    got = await f.find_video_url(Worker(ctx), OID)
    check("15 find_video_url HTTP 优先且零新页面", got == FRESH and ctx.new_pages == 0,
          (got[:50], ctx.new_pages))
    check("16 worker=None 也能取", (await f.find_video_url(None, OID)) == FRESH)
    restore()

    # 17) HTTP 失败 + 无 context -> 返回空且不新开页面
    install({1: {"errCode": 0, "data": {"list": [], "total": 0}}})
    ctx = Ctx()
    got = await fetcher().find_video_url(Worker(None), OID)
    check("17 HTTP 失败且 context=None 直接空", got == "" and ctx.new_pages == 0, (got, ctx.new_pages))
    restore()

    # 18) 服务端源码断言
    sv = io.open(os.path.join(ROOT, "backend", "server.py"), encoding="utf-8").read()
    check("18 播放接口按 config 找账号", "acc = _account_for_finder(finder_id)" in sv)
    check("19 不再以 w.logged_in 为前置", "if w is not None and w.logged_in:" not in sv)
    check("20 写库前验可播", "if fresh and await asyncio.to_thread(_url_playable, fresh):" in sv)
    check("21 旧误导文案已删", "视频号未运行/作品太旧不在列表前几页" not in sv)

    # 22) 失败提示映射（AST 抽出纯函数单独 exec，不 import server 依赖）
    tree = ast.parse(sv)
    ns = {}
    wanted = {"_PLAY_FAIL_MSG": ast.Assign, "_play_fail_message": ast.FunctionDef}
    for node in tree.body:
        name = ""
        if isinstance(node, ast.Assign):
            name = node.targets[0].id if isinstance(node.targets[0], ast.Name) else ""
        elif isinstance(node, ast.FunctionDef):
            name = node.name
        if name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<msg>", "exec"), ns)
    fn = ns.get("_play_fail_message")
    check("22 提示函数可独立加载", callable(fn))
    if callable(fn):
        check("23 登录态过期提示准确", "重新扫码" in fn("errCode:300333"), fn("errCode:300333"))
        check("24 作品已删除提示准确", "不在视频号作品列表" in fn("not_in_list"), fn("not_in_list"))
        check("25 不再误报未运行", "未运行" not in fn("post_list_unavailable"), fn("post_list_unavailable"))


if __name__ == "__main__":
    asyncio.run(run())
    bad = [n for n, ok, _ in RESULT if not ok]
    print("\n共 %d 项，失败 %d 项" % (len(RESULT), len(bad)))
    if bad:
        print("FAILED:", bad)
    raise SystemExit(1 if bad else 0)
