# -*- coding: utf-8 -*-
"""rev50 验收: 用母本账号 cookie 在本地直连 comment_list / post_list，确认 HTTP 路径拿到的
数据与浏览器路径结构一致（字段齐全、errCode=0、条数合理）。

这是 rev48 用过的「双路对拍」办法：不去猜，直接看同一份登录态下 HTTP 能拿到什么。
"""
import json
import os
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

import asyncio  # noqa: E402
from backend import wx_http  # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
BASE_I = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_I = "https://channels.weixin.qq.com/micro/interaction/comment"
BASE_C = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin"
PAGE_C = "https://channels.weixin.qq.com/platform/post/list"


def pick():
    cfg = json.load(open(CFG, encoding="utf-8"))
    cands = []
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        ck = wx_http.build_cookie_header(ss)
        if ck and a.get("_aid") and a.get("_log_finder_id"):
            cands.append((a, ck))
    return cands


async def main():
    print("HTTP 后端:", wx_http.backend_name())
    cands = pick()
    print("有可用登录态的账号: %d 个" % len(cands))
    if not cands:
        print("没有可测账号（都没扫过码）")
        return

    for a, ck in cands[:3]:
        aid, fid = a.get("_aid"), a.get("_log_finder_id")
        print("\n===== %s (aid=%s..., fid=%s) =====" % (a.get("id"), aid[:8], fid[:10]))
        print("  cookie: %d 字节, 名称: %s"
              % (len(ck), ",".join(sorted(x.split("=")[0] for x in ck.split("; ")))))

        # 1) 评论页的 post_list
        url = wx_http.api_url(BASE_I, "post/post_list", aid, PAGE_I)
        body = wx_http.std_body(fid, pageSize=20, onlyUnread=False,
                                needAllCommentCount=True, userpageType=13,
                                forMcn=False, lastBuff="")
        t0 = time.time()
        j = await wx_http.post_json(a, url, body, page_url=PAGE_I, timeout=25)
        dt = time.time() - t0
        err = j.get("__err") if isinstance(j, dict) else None
        code = j.get("errCode") if isinstance(j, dict) else None
        lst = (((j.get("data") or {}).get("list")) or []) if isinstance(j, dict) else []
        print("  [1] interaction/post_list  %.2fs  __err=%s errCode=%s  条数=%d"
              % (dt, err, code, len(lst)))
        if lst:
            v = lst[0]
            print("      首条字段: %s" % ",".join(sorted(v.keys()))[:200])
            print("      objectId=%s commentCount=%s" % (v.get("objectId"), v.get("commentCount")))
            # 2) 用第一条的 objectId 拉评论，验证 comment_list
            oid = v.get("objectId")
            body2 = wx_http.std_body(fid, lastBuff="", exportId=oid,
                                     commentSelection=False, forMcn=False)
            url2 = wx_http.api_url(BASE_I, "comment/comment_list", aid, PAGE_I)
            t0 = time.time()
            j2 = await wx_http.post_json(a, url2, body2, page_url=PAGE_I, timeout=25)
            dt2 = time.time() - t0
            cm = (((j2.get("data") or {}).get("comment")) or [])
            print("  [2] comment/comment_list  %.2fs  __err=%s errCode=%s  条数=%d lastBuff=%s"
                  % (dt2, j2.get("__err"), j2.get("errCode"), len(cm),
                     bool((j2.get("data") or {}).get("lastBuff"))))
            if cm:
                c = cm[0]
                print("      首条字段: %s" % ",".join(sorted(c.keys()))[:200])

        # 3) 内容管理页的 post_list（作品抓取用的那条）
        url3 = wx_http.api_url(BASE_C, "post/post_list", aid, PAGE_C)
        body3 = wx_http.std_body(fid, pageSize=5, currentPage=1,
                                 userpageType=11, stickyOrder=True)
        t0 = time.time()
        j3 = await wx_http.post_json(a, url3, body3, page_url=PAGE_C, timeout=25)
        dt3 = time.time() - t0
        lst3 = (((j3.get("data") or {}).get("list")) or [])
        print("  [3] content/post_list     %.2fs  __err=%s errCode=%s  条数=%d"
              % (dt3, j3.get("__err"), j3.get("errCode"), len(lst3)))
        if lst3:
            p = lst3[0]
            ms = ((p.get("desc") or {}).get("media") or [])
            print("      objectId=%s  有封面=%s  有播放地址=%s  visibleType=%s"
                  % (p.get("objectId"), bool((ms[0] if ms else {}).get("coverUrl")),
                     bool((ms[0] if ms else {}).get("url")), p.get("visibleType")))

    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
