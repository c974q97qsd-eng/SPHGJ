# -*- coding: utf-8 -*-
"""rev73 单测：写成功独立 INFO 日志 + 读写「首次成功」标记分开。

本机没装 playwright，但 backend.api_client 可真实 import；wx_http 用假模块替换，
不向微信发任何请求。所有异步用例放在同一个 asyncio.run 里（账号锁按 loop 隔离）。
"""
import asyncio
import io
import logging
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backend.api_client as AC
from backend.api_client import WxApiClient, reset_account_locks_for_test

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, extra))


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, r):
        self.lines.append(r.getMessage())

    def take(self, kw):
        return [x for x in self.lines if kw in x]

    def clear(self):
        self.lines = []


LOG = logging.getLogger("sphgj")
CAP = Capture()
LOG.addHandler(CAP)
LOG.setLevel(logging.INFO)


class LivePage:
    def is_closed(self):
        return False


class FakeHttp:
    """替换 AC.wx_http：不发包，直接返回成功报文。"""
    def __init__(self, resp=None):
        self.resp = resp or {"errCode": 0, "data": {}}
        self.calls = []

    async def post_json(self, acc, url, body, page_url=None, timeout=30):
        self.calls.append((url, body))
        return self.resp

    def backend_name(self):
        return "fake"

    def cookie_header_of(self, acc):
        return "cookie=stub"


REAL_HTTP = AC.wx_http
AC.wx_http = FakeHttp()


def mk(acc_id="t-a", via=None, page=None, storage=None):
    cfg = {}
    rc = {}
    if via:
        rc["api_write_via"] = via
    if rc:
        cfg["risk_control"] = rc
    return WxApiClient(page,
                       {"id": acc_id, "_aid": "aid-" + acc_id,
                        "_log_finder_id": "fid-" + acc_id},
                       cfg, storage)


def stub_impl(a, resp, throttle_sleep=0.0, http=True):
    """把请求通道换成本地桩；返回 None 表示让上层走 page 分支。"""
    async def fake_http(path, url, body, page_url, is_write=False):
        return resp if http else None

    async def fake_page(path, url, body):
        return resp

    async def fake_throttle(path):
        if throttle_sleep:
            await asyncio.sleep(throttle_sleep)
    a._post_via_http = fake_http
    a._post_via_page = fake_page
    a._throttle = fake_throttle


async def main_async():
    src = io.open(os.path.join(ROOT, "backend", "api_client.py"),
                  encoding="utf-8").read()

    # ---------- A 静态结构 ----------
    print("== A 源码结构 ==")
    check("A1 __init__ 初始化读首次标记", "self._http_read_logged = False" in src)
    check("A2 __init__ 初始化写首次标记", "self._http_write_logged = False" in src)
    check("A3 保留 _http_logged 兼容标记", "self._http_logged = False" in src)
    check("A4 写成功日志含 5 个字段",
          "写操作成功 path=%s account=%s via=%s errCode=%s 耗时=%.2fs" in src)
    check("A5 请求阶段计时不含节流/锁",
          "t_req = time.perf_counter()" in src and
          "cost = time.perf_counter() - t_req" in src and
          src.index("await self._throttle(path)") < src.index("t_req = time.perf_counter()"))
    check("A6 旧的单 flag 门控已移除", "if not self._http_logged:" not in src)
    check("A7 首次写日志独立于读 flag",
          "首次写操作成功 path=" in src and
          "self._http_write_logged = True" in src)

    # ---------- B _post_impl 写成功日志 ----------
    print("== B 写成功逐条 INFO ==")
    a = mk("acc-b")
    stub_impl(a, {"errCode": 0, "data": {"ok": 1}})
    CAP.clear()
    r = await a._post("comment/del_comment", {"exportId": "x"})
    wl = CAP.take("写操作成功")
    check("B1 写成功打 1 条 INFO", len(wl) == 1, repr(CAP.lines))
    line = wl[0] if wl else ""
    check("B2 含 path", "path=comment/del_comment" in line, line)
    check("B3 含账号", "account=acc-b" in line, line)
    check("B4 含 via=http", "via=http" in line, line)
    check("B5 含 errCode=0", "errCode=0" in line, line)
    m = re.search(r"耗时=([0-9.]+)s", line)
    check("B6 含数值耗时", bool(m), line)
    check("B7 请求成功未被误判失败", r.get("errCode") == 0, repr(r))

    # 读成功不产生写日志
    b = mk("acc-b2")
    stub_impl(b, {"errCode": 0})
    CAP.clear()
    await b._post("post/post_list", {})
    check("B8 读成功不打写成功日志", len(CAP.take("写操作成功")) == 0, repr(CAP.lines))

    # 各类失败 / 零发包不得记成写成功
    for tag, resp in (("B9 服务端 __err", {"__err": "http_errCode=300800"}),
                      ("B10 daily_write_limit", {"__err": "daily_write_limit"}),
                      ("B11 no_cookie", {"__err": "no_cookie"}),
                      ("B12 http_blocked", {"__err": "http_blocked"})):
        c = mk("acc-" + tag[:3])
        stub_impl(c, resp)
        CAP.clear()
        await c._post("comment/create_comment", {"content": "x"})
        check(tag + " 不打写成功日志", len(CAP.take("写操作成功")) == 0,
              repr(CAP.lines))

    # 与读 flag 分开：_http_logged 已被读占用也照打
    d = mk("acc-d")
    d._http_logged = True
    stub_impl(d, {"errCode": 0})
    CAP.clear()
    await d._post("comment/del_comment", {"exportId": "x"})
    check("B13 读 flag 已 True 仍打写成功日志",
          len(CAP.take("写操作成功")) == 1, repr(CAP.lines))

    # 耗时不含节流随机等待
    e = mk("acc-e")
    stub_impl(e, {"errCode": 0}, throttle_sleep=0.25)
    CAP.clear()
    await e._post("comment/del_comment", {"exportId": "x"})
    el = CAP.take("写操作成功")
    m2 = re.search(r"耗时=([0-9.]+)s", el[0] if el else "")
    check("B14 耗时不含 0.25s 节流等待",
          bool(m2) and float(m2.group(1)) < 0.15,
          el[0] if el else "no line")

    # 浏览器回退通道也能记（via=page）
    f = mk("acc-f", via="browser", page=LivePage())
    stub_impl(f, {"errCode": 0}, http=False)
    CAP.clear()
    await f._post("comment/del_comment", {"exportId": "x"})
    pl = CAP.take("写操作成功")
    check("B15 page 通道也打日志", len(pl) == 1, repr(CAP.lines))
    check("B16 page 通道标 via=page", bool(pl) and "via=page" in pl[0],
          pl[0] if pl else "")

    # errCode 缺失时显示 none
    g = mk("acc-g")
    stub_impl(g, {"data": {}})
    CAP.clear()
    await g._post("comment/del_comment", {"exportId": "x"})
    gl = CAP.take("写操作成功")
    check("B17 errCode 缺失显示 none", bool(gl) and "errCode=none" in gl[0],
          gl[0] if gl else "")

    # ---------- C _post_via_http 首次日志读写分开 ----------
    print("== C 首次成功日志读写独立 ==")
    AC.wx_http = FakeHttp()
    h = mk("acc-h")
    CAP.clear()
    await h._post_via_http("post/post_list", "u", {}, "p", is_write=False)
    await h._post_via_http("comment/del_comment", "u", {}, "p", is_write=True)
    check("C1 读先成功后写仍打首次写日志",
          len(CAP.take("首次写操作成功")) == 1, repr(CAP.lines))
    check("C2 读首次日志打了 1 条",
          len(CAP.take("首次读操作成功")) == 1, repr(CAP.lines))
    check("C3 兼容标记 _http_logged 仍为 True", h._http_logged is True)
    check("C4 写首次标记已置位", h._http_write_logged is True)
    await h._post_via_http("comment/del_comment", "u", {}, "p", is_write=True)
    check("C5 首次写日志只打 1 次",
          len(CAP.take("首次写操作成功")) == 1, repr(CAP.lines))
    await h._post_via_http("post/post_list", "u", {}, "p", is_write=False)
    check("C6 首次读日志只打 1 次",
          len(CAP.take("首次读操作成功")) == 1, repr(CAP.lines))

    i = mk("acc-i")
    CAP.clear()
    await i._post_via_http("comment/create_comment", "u", {}, "p", is_write=True)
    check("C7 写先成功也打首次写日志",
          len(CAP.take("首次写操作成功")) == 1, repr(CAP.lines))
    check("C8 写先成功不会误打「首次读」日志",
          len(CAP.take("首次读操作成功")) == 0, repr(CAP.lines))

    # 裸实例（没跑 __init__）不能因新 flag 抛 AttributeError
    bare = object.__new__(WxApiClient)
    bare.account = {"id": "bare", "_aid": "aid-bare"}
    bare.account_id = "bare"
    bare.aid = "aid-bare"
    bare.config = {}
    bare.auth_fail = None
    bare._http_block_until = 0.0
    bare._http_last_why = ""
    bare._http_logged = False
    try:
        br = await bare._post_via_http("comment/del_comment", "u", {}, "p",
                                       is_write=True)
        check("C9 裸实例不因新 flag 崩", isinstance(br, dict), repr(br))
    except Exception as ex:
        check("C9 裸实例不因新 flag 崩", False, repr(ex))

    # ---------- D 账号锁回归（同账号串行 / 多账号并行） ----------
    print("== D 账号隔离锁回归 ==")
    reset_account_locks_for_test()
    events = []

    async def _noop_throttle(p):
        return None

    def instrument(x, key, delay=0.06):
        async def fh(path, url, body, page_url, is_write=False):
            events.append(("enter", key, time.perf_counter()))
            await asyncio.sleep(delay)
            events.append(("exit", key, time.perf_counter()))
            return {"errCode": 0}
        x._post_via_http = fh
        x._throttle = _noop_throttle

    x1, x2 = mk("acc-same"), mk("acc-same")
    instrument(x1, "same1")
    instrument(x2, "same2")
    await asyncio.gather(x1._post("comment/del_comment", {}),
                         x2._post("comment/del_comment", {}))
    seq = [e[0] for e in events]
    check("D1 同账号两次写严格串行",
          seq == ["enter", "exit", "enter", "exit"], repr(seq))

    events.clear()
    CAP.clear()
    y1, y2 = mk("acc-A"), mk("acc-B")
    instrument(y1, "A")
    instrument(y2, "B")
    await asyncio.gather(y1._post("comment/del_comment", {}),
                         y2._post("comment/del_comment", {}))
    seq2 = [e[0] for e in events]
    check("D2 不同账号可并行(第二个 enter 在第一个 exit 前)",
          seq2[:2] == ["enter", "enter"], repr(seq2))
    check("D3 并行写各打一条成功日志",
          len(CAP.take("写操作成功")) == 2, repr(CAP.lines))


def main():
    asyncio.run(main_async())
    AC.wx_http = REAL_HTTP
    LOG.removeHandler(CAP)
    print("\n合计: PASS=%d FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
