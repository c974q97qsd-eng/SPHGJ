# -*- coding: utf-8 -*-
"""排查：item/list 能不能翻页、能不能覆盖全部作品。

上一轮结论是「只返回 19 条、max_cursor=0、翻不了页」，但抓包里的真实响应
顶层字段是 `cursor`（时间戳字符串），不是 `max_cursor` —— 极可能是我们读错了
字段名，不是服务端不分页。本脚本直接用原始 fetch 验证：

  1. 第 1 页拿到多少条、`cursor` 是什么、`has_more` 是什么
  2. 拿 `cursor` 当参数翻第 2/3 页，看是不是真的换了一批
  3. 累计能不能覆盖 work_list 的全部作品
  4. 顺带试 `count` 放大 / 不传 count 的差异
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402
from cdp_page import CdpPage  # noqa: E402

CTX = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, CTX)
from backend.douyin.client import DouyinClient  # noqa: E402
from backend.douyin import api  # noqa: E402

PAGE = "https://creator.douyin.com/creator-micro/interactive/comment"


def hr(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


async def main():
    t = cdp.pick()
    if not t:
        print("没找到 creator.douyin.com 页面，请先确保 9222 浏览器开着该站")
        return
    c = cdp.CDP(t["webSocketDebuggerUrl"])
    page = CdpPage(c)
    cli = DouyinClient(page, {"id": "dyprobe"})
    cli.read_interval = (0, 0)

    # ---------------- 1. work_list 全量 ----------------
    hr("[1] work_list 全量（作为覆盖基准）")
    works, total = await cli.fetch_all_works(count=12)
    plain_ids = [str(w.get("aweme_id") or w.get("id") or "") for w in works]
    plain_ids = [x for x in plain_ids if x]
    print("work_list 作品数 = %d, total=%s" % (len(plain_ids), total))

    # ---------------- 2. item/list 首页原始字段 ----------------
    hr("[2] item/list 首页原始字段")
    env = await cli.fetch_item_page(cursor="", count=20)
    if not env["__ok"]:
        print("请求失败:", env)
        return
    raw = env["raw"]
    print("响应顶层键:", sorted(raw.keys()))
    print("cursor      =", repr(raw.get("cursor")))
    print("max_cursor  =", repr(raw.get("max_cursor")))
    print("has_more    =", repr(raw.get("has_more")))
    print("item_info_list 条数 =", len(raw.get("item_info_list") or []))
    print("本页映射条数 =", len(env["map"]))

    # ---------------- 3. 拿 cursor 翻页 ----------------
    hr("[3] 用响应 cursor 翻页（最多 12 页）")
    seen = {}
    cur = ""
    pages = 0
    log = []
    while pages < 12:
        e = await cli.fetch_item_page(cursor=cur, count=20)
        pages += 1
        if not e["__ok"]:
            log.append((pages, 0, cur, "ERR " + str(e.get("__err"))))
            break
        r = e["raw"]
        m = e["map"]
        newcnt = len([k for k in m if k not in seen])
        seen.update(m)
        nxt = str(r.get("cursor") or "")
        more = bool(r.get("has_more"))
        log.append((pages, len(m), cur[:18], "new=%d cursor->%s has_more=%s"
                    % (newcnt, nxt[:18], more)))
        print("第%d页: 本页%d 新增%d 累计%d cursor=%r has_more=%s"
              % (pages, len(m), newcnt, len(seen), nxt[:20], more))
        if not more or not nxt or nxt == cur or newcnt == 0:
            break
        cur = nxt

    print("\n累计映射 %d 条 / work_list %d 条" % (len(seen), len(plain_ids)))
    miss = [x for x in plain_ids if x not in seen]
    print("缺映射 %d 条" % len(miss))
    extra = [x for x in seen if x not in set(plain_ids)]
    print("item/list 有但 work_list 没有 = %d 条" % len(extra))

    # ---------------- 4. count 放大是否有效 ----------------
    hr("[4] count 参数是否生效")
    for cnt in (10, 20, 50, 100):
        e = await cli.fetch_item_page(cursor="", count=cnt)
        n = len(e.get("map") or {}) if e["__ok"] else -1
        print("count=%-4d -> 本页 %d 条 (ok=%s)" % (cnt, n, e["__ok"]))
        time.sleep(0.6)

    # ---------------- 5. 不传 count（照抄原站） ----------------
    hr("[5] 照抄原站形态：?cursor=&aid=2906 （不带 count）")
    url = api.PATH_ITEM_LIST + "?cursor=&aid=%d" % api.AID
    e = await cli._request("GET", url)
    if e["__ok"]:
        r = e["raw"]
        print("条数 =", len(r.get("item_info_list") or []),
              "cursor =", repr(r.get("cursor")),
              "has_more =", repr(r.get("has_more")))
    else:
        print("失败:", e)

    hr("结论")
    ok = len(miss) == 0
    print("能否覆盖全部作品：%s" % ("能" if ok else "不能"))
    if not ok:
        print("样本缺失 id:", miss[:5])


import asyncio  # noqa: E402
asyncio.run(main())
