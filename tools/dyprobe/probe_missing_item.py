# -*- coding: utf-8 -*-
"""看那 1 条「work_list 有、item/list 没有」的作品到底是什么。

判断它是不是：
  · 置顶作品（置顶会改变排序，可能被 item/list 漏掉）
  · 图文 / 直播回放（类型不同，评论接口可能要别的 id）
  · 私密 / 审核中（本来就回复不了，缺映射不算 bug）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa: E402
from cdp_page import CdpPage  # noqa: E402

CTX = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, CTX)
from backend.douyin.client import DouyinClient  # noqa: E402

MISS = "7461649401077845302"


async def main():
    t = cdp.pick()
    if not t:
        print("没找到 creator.douyin.com 页面")
        return
    c = cdp.CDP(t["webSocketDebuggerUrl"])
    cli = DouyinClient(CdpPage(c), {"id": "dyprobe"}, {"risk_control": {"dy_read_interval": [0, 0]}})

    works, _ = await cli.fetch_all_works(count=12)
    print("work_list 共 %d 条" % len(works))
    for i, w in enumerate(works):
        aid = str(w.get("aweme_id") or "")
        if aid == MISS:
            print("\n找到目标，序号 %d / %d" % (i + 1, len(works)))
            keep = ("aweme_id", "desc", "create_time", "is_top", "status_value",
                    "media_type", "aweme_type", "duration", "is_image", "images",
                    "statistics", "permission_status", "in_review", "is_delete",
                    "video", "image_post_info")
            for k in keep:
                if k in w:
                    v = w[k]
                    if isinstance(v, dict):
                        print("  %-16s = %s" % (k, json.dumps(v, ensure_ascii=False)[:160]))
                    elif isinstance(v, list):
                        print("  %-16s = list(%d)" % (k, len(v)))
                    else:
                        print("  %-16s = %r" % (k, v))
            print("  全部键 =", sorted(w.keys()))
            break
    else:
        print("work_list 里没有这条 id")

    # 打印 work_list 前 8 条 id，便于和 item/list 首页对照
    print("\nwork_list 前 8 条:")
    for w in works[:8]:
        print("   %s  is_top=%s  %s" % (w.get("aweme_id"), w.get("is_top"),
                                        str(w.get("desc") or "")[:24]))

    # item/list 首页 19 条 id
    e = await cli.fetch_item_page(cursor="", count=20)
    if e["__ok"]:
        print("\nitem/list 首页 id:")
        for k in list(e["map"].keys()):
            print("   " + k)


import asyncio  # noqa: E402
asyncio.run(main())
