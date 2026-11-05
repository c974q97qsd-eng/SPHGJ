# -*- coding: utf-8 -*-
"""rev65 单测：播放链路纯 HTTP + 页码直跳 + 8s 上限。

沿用 rev64 单测的 fake 内核(wx_http.post_json / cookie_header_of 顶掉,绝不出网)；
server 侧用 AST 抽出 post_play 单独 exec —— 本机没装 playwright,不 import server。
namespace 里故意不注入 _url_playable / manager / find_video_url:代码要是还碰它们,
调用时必然 NameError,等价于「零 CDN 探测 / 零浏览器」的强断言。
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


class PlainStorage:
    """没有 _conn 的旧库 / stub:估算退化为第 1 页。"""
    db_path = r"D:\SPHGJ\data\test.db"


class Cur:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class RankStorage(PlainStorage):
    """带 create_time 排名的库:模拟 SELECT create_time / SELECT COUNT(*)。"""

    def __init__(self, newer=2268, create_time=1755454179):
        self.newer = newer
        self.create_time = create_time

    def _conn(self):
        return self

    def execute(self, sql, params=()):
        if "SELECT create_time" in sql:
            return Cur((self.create_time,))
        return Cur((self.newer,))

    def close(self):
        pass


def fetcher(acc=None, cfg=None, store=None):
    a = acc or account()
    return pfmod.PostFetcher(store or PlainStorage(), a,
                             cfg or {"accounts": [a]})


def page(pg, hit=False, n=200, total=2587, err_code=0):
    lst = []
    for i in range(n):
        oid = OID if (hit and i == 0) else "export/OTHER_%d_%d" % (pg, i)
        lst.append({"objectId": oid, "exportId": oid,
                    "desc": {"media": [{"url": FRESH if oid == OID else "https://cdn/old"}]}})
    return {"errCode": err_code, "data": {"list": lst, "total": total}}


def install(pages, calls=None):
    """pages: {页码: 返回dict} 或 callable(pg)->dict。"""
    async def fake(acc, url, body, page_url="", headers=None, timeout=20):
        pg = int(body.get("currentPage") or 1)
        if calls is not None:
            calls.append(pg)
        if callable(pages):
            return pages(pg)
        return pages.get(pg, {"errCode": 0, "data": {"list": [], "total": 0}})
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


# ==================== server.post_play 的独立 exec 环境 ====================
class FakeApp:
    def get(self, *a, **kw):
        return lambda f: f


class FakeHTTPException(Exception):
    pass


class FakeLogger:
    def warning(self, *a, **kw):
        pass


class Store:
    def __init__(self, row="DEFAULT"):
        self.row = ({"title": "测试作品", "video_url": ""}
                    if row == "DEFAULT" else row)
        self.writes = []

    def get_post_video(self, fid, oid):
        return self.row

    def update_post_video(self, fid, oid, url):
        self.writes.append((fid, oid, url))


class FakeFetcher:
    """可控的 PostFetcher：返回 (url, why) / 抛异常 / 永远不结束。"""
    outcome = (FRESH, "")
    calls = []
    wait_for_timeouts = []

    def __init__(self, storage, acc, config):
        self.storage = storage
        self.acc = acc
        self.config = config

    async def find_video_url_http(self, oid):
        FakeFetcher.calls.append(oid)
        out = FakeFetcher.outcome
        if isinstance(out, BaseException):
            raise out
        if out == "HANG":
            await asyncio.sleep(60)
        return out


class FakeAsyncio:
    TimeoutError = asyncio.TimeoutError

    @staticmethod
    async def wait_for(coro, timeout):
        FakeAsyncio.seen_timeout = timeout
        return await asyncio.wait_for(coro, timeout=timeout)


def load_play(sv_source, store, acc, wait_timeout=8.0):
    tree = ast.parse(sv_source)
    node = next(n for n in tree.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "post_play")
    ns = {
        "app": FakeApp(),
        "Query": lambda default="": default,
        "HTTPException": FakeHTTPException,
        "logger": FakeLogger(),
        "storage": store,
        "config": {"accounts": [acc] if acc else []},
        "_account_for_finder": lambda fid: acc,
        "_play_fail_message": lambda why: "原因:" + str(why),
        "PostFetcher": FakeFetcher,
        "asyncio": FakeAsyncio,
        "str": str,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<play>", "exec"), ns)
    return ns["post_play"], ns


async def run():
    # ---- A. 页码估算 ----
    f = fetcher(store=RankStorage(newer=2268))
    check("1 按排名估算页码 12", f._guess_start_page(OID, 200) == 12,
          f._guess_start_page(OID, 200))
    check("2 无 _conn 退化为第 1 页", fetcher()._guess_start_page(OID, 200) == 1)
    check("3 create_time 为空退化第 1 页",
          fetcher(store=RankStorage(newer=10, create_time=0))._guess_start_page(OID, 200) == 1)

    # ---- B. 直跳命中：只发 1 次请求 ----
    calls = []
    install({12: page(12, hit=True)}, calls)
    url, why = await fetcher(store=RankStorage()).find_video_url_http(OID)
    check("4 估算第 12 页一次命中", url == FRESH and why == "", (url[:50], why))
    check("5 只发 1 次请求(不再翻 12 页)", calls == [12], calls)
    check("6 URL 完整未截断", len(url) == len(FRESH) and url.endswith("&sign=END"), len(url))
    restore()

    # ---- C. 前后容错 ----
    for delta, label in ((1, "7 命中估算页+1"), (-1, "8 命中估算页-1"),
                         (2, "9 命中估算页+2")):
        calls = []
        hitpg = 12 + delta
        pages = {p: page(p) for p in range(10, 15)}
        pages[hitpg] = page(hitpg, hit=True)
        install(pages, calls)
        url, why = await fetcher(store=RankStorage()).find_video_url_http(OID)
        check(label, url == FRESH and calls[0] == 12 and hitpg in calls[:5],
              (url[:40], calls[:5]))
        restore()

    # ---- D. 置顶作品 / 空页容错 ----
    calls = []
    install({1: page(1, hit=True)}, calls)   # 置顶作品:只有第 1 页有数据
    url, why = await fetcher(store=RankStorage(newer=2630)).find_video_url_http(OID)
    check("10 置顶作品在第 1 页仍命中", url == FRESH and 1 in calls[:2],
          (url[:40], calls[:6]))
    check("10b 估算超界时空页不误判到底", why == "", why)
    restore()
    calls = []
    install({12: {"errCode": 0, "data": {"list": [], "total": 2587}},
             13: page(13, hit=True)}, calls)
    url, why = await fetcher(store=RankStorage()).find_video_url_http(OID)
    check("11 估算页空仍继续并命中", url == FRESH and 13 in calls[:5],
          (url[:40], calls[:6]))
    restore()
    calls = []
    install(lambda pg: {"errCode": 0, "data": {"list": [], "total": 2587}}, calls)
    url, why = await fetcher(store=RankStorage()).find_video_url_http(OID)
    check("12 全空时判 not_in_list", url == "" and why == "not_in_list",
          (why, len(calls)))
    check("12b 全空时请求数受控", len(calls) <= 10, calls)
    restore()

    # ---- E. 估算全偏时仍会顺序补齐 ----
    calls = []
    install(lambda pg: page(pg, hit=(pg == 3), total=100000), calls)
    url, why = await fetcher(store=RankStorage(newer=2268)).find_video_url_http(OID)
    check("13 估算全偏时补齐命中", url == FRESH and 3 in calls, (url[:40], calls[:8]))
    restore()

    # ---- F. 失败原因 & 不发包 ----
    calls = []
    install({12: page(12, hit=True)}, calls)
    check("14 无 cookie 报准确原因且不发包",
          (await fetcher(acc=account(with_cookie=False),
                         store=RankStorage()).find_video_url_http(OID)) == ("", "no_cookie")
          and calls == [])
    check("15 无 _aid 报准确原因且不发包",
          (await fetcher(acc=account(with_aid=False),
                         store=RankStorage()).find_video_url_http(OID)) == ("", "no_aid")
          and calls == [])
    check("16 空 object_id 不发包",
          (await fetcher(store=RankStorage()).find_video_url_http("")) == ("", "empty_object_id")
          and calls == [])
    restore()
    install({12: page(12, err_code=300333)})
    check("17 errCode 原样上报",
          (await fetcher(store=RankStorage()).find_video_url_http(OID))[1] == "errCode:300333")
    restore()
    install({12: {"__err": "no_cookie"}})
    check("18 __err 原样上报",
          (await fetcher(store=RankStorage()).find_video_url_http(OID))[1] == "http:no_cookie")
    restore()
    async def boom(*a, **kw):
        raise RuntimeError("network down")
    pfmod.wx_http.post_json = boom
    pfmod.wx_http.cookie_header_of = lambda acc: "sessionid=x"
    check("19 传输异常原样上报",
          (await fetcher(store=RankStorage()).find_video_url_http(OID))[1]
          == "request_error:RuntimeError")
    restore()
    calls = []
    install(lambda pg: page(pg), calls)
    url, why = await fetcher(store=RankStorage(newer=0)).find_video_url_http(OID, max_pages=3)
    check("20 max_pages 上限生效", url == "" and why == "page_limit" and len(calls) == 3,
          (why, calls))
    restore()

    # ---- G. find_video_url 仍 HTTP 优先、不碰浏览器 ----
    install({12: page(12, hit=True)})
    ctx = Ctx()
    check("21 find_video_url HTTP 优先且零新页面",
          (await fetcher(store=RankStorage()).find_video_url(Worker(ctx), OID)) == FRESH
          and ctx.new_pages == 0)
    restore()

    # ---- H. server.post_play（AST 独立 exec）----
    sv = io.open(os.path.join(ROOT, "backend", "server.py"), encoding="utf-8").read()
    check("22 源码无同步 CDN 探测",
          "_url_playable" not in sv.split('@app.get("/api/posts/play")')[1]
          .split('@app.websocket("/api/remote/ws")')[0])
    check("23 源码无浏览器兜底",
          "find_video_url(w, object_id)" not in sv)
    check("24 源码 8s 总时限", "timeout=8.0" in sv)

    store = Store()
    play, ns = load_play(sv, store, account())
    FakeFetcher.calls = []
    FakeFetcher.outcome = (FRESH, "")
    r = await play(FID, OID)
    check("25 纯 HTTP 一路返回成功", r.get("ok") is True and r.get("url") == FRESH, r)
    check("26 只调一次 find_video_url_http", FakeFetcher.calls == [OID], FakeFetcher.calls)
    check("27 写回新地址", store.writes == [(FID, OID, FRESH)], store.writes)

    FakeFetcher.calls = []
    store2 = Store()
    play2, _ = load_play(sv, store2, None)
    r = await play2(FID, OID)
    check("28 找不到账号给准确原因且零请求",
          r.get("ok") is False and "原因:no_account_for_finder" in r.get("message", "")
          and FakeFetcher.calls == [], (r, FakeFetcher.calls))

    FakeFetcher.outcome = ("", "not_in_list")
    r = await play(FID, OID)
    check("29 现取失败映射成人话", r.get("ok") is False
          and "原因:not_in_list" in r.get("message", ""), r)

    FakeFetcher.outcome = RuntimeError("boom")
    r = await play(FID, OID)
    check("30 异常不炸接口", r.get("ok") is False and "RuntimeError" in r.get("message", ""), r)

    FakeFetcher.outcome = "HANG"

    class QuickAsyncio(FakeAsyncio):
        seen_timeout = None

        @staticmethod
        async def wait_for(coro, timeout):
            QuickAsyncio.seen_timeout = timeout
            coro.close()
            raise asyncio.TimeoutError()

    ns["asyncio"] = QuickAsyncio
    r = await play(FID, OID)
    check("31 超时返回明确提示不无限转圈",
          r.get("ok") is False and "超时" in r.get("message", ""), r)
    check("32 wait_for 收到 8.0 秒上限", QuickAsyncio.seen_timeout == 8.0,
          QuickAsyncio.seen_timeout)
    ns["asyncio"] = FakeAsyncio
    FakeFetcher.outcome = (FRESH, "")

    store3 = Store(row=None)
    play4, _ = load_play(sv, store3, account())
    try:
        await play4(FID, OID)
        check("33 作品不存在抛 404", False, "没抛")
    except FakeHTTPException:
        check("33 作品不存在抛 404", True)
    try:
        await play4("", OID)
        check("34 缺参数抛 400", False, "没抛")
    except FakeHTTPException:
        check("34 缺参数抛 400", True)

    return 0 if all(x[1] for x in RESULT) else 1


if __name__ == "__main__":
    rc = asyncio.run(run())
    print("\n%d/%d PASS" % (sum(1 for x in RESULT if x[1]), len(RESULT)))
    sys.exit(rc)
