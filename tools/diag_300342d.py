# -*- coding: utf-8 -*-
"""诊断 300342 第四阶段：timestamp（防重放）/ 跨接口并发。

已排除：
    _aid / _pageUrl / _log_finder_id 错配   -> errCode=0
    sessionid 篡改                          -> errCode=300330
    同接口并发 4 / 串行 0.3s 连发 8          -> errCode=0

现场特征是「同一秒、两个不同接口、同一账号」都被拒，所以再打两个方向：
    T1 timestamp 落后 5 分钟      （时钟漂移 / 防重放窗口）
    T2 timestamp 超前 5 分钟
    T3 两个请求用完全相同的 timestamp（串行、间隔 1s，模拟同毫秒生成）
    T4 跨接口并发（interaction 与 content 两条不同 base 路径同时打）
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http  # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
BASE_I = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
BASE_C = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin"
PAGE_I = "https://channels.weixin.qq.com/micro/interaction/comment"
PAGE_C = "https://channels.weixin.qq.com/platform/post/list"


def pick():
    cfg = json.load(open(CFG, encoding="utf-8"))
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        if wx_http.build_cookie_header(ss) and a.get("_aid") and a.get("_log_finder_id"):
            return a
    return None


async def req(acc, tag, base=BASE_I, page=PAGE_I, **body_extra):
    aid = acc["_aid"]
    fid = acc["_log_finder_id"]
    url = wx_http.api_url(base, "post/post_list", aid, page)
    if base is BASE_C:
        body = wx_http.std_body(fid, pageSize=5, currentPage=1,
                                userpageType=11, stickyOrder=True, **body_extra)
    else:
        body = wx_http.std_body(fid, pageSize=5, onlyUnread=False,
                                needAllCommentCount=True, userpageType=13,
                                forMcn=False, lastBuff="", **body_extra)
    t0 = time.time()
    try:
        j = await wx_http.post_json(acc, url, body, page_url=page, timeout=25)
        code = j.get("errCode") if isinstance(j, dict) else None
        msg = j.get("errMsg") if isinstance(j, dict) else None
    except Exception as e:
        code, msg = "EXC", repr(e)[:40]
    return tag, code, msg, time.time() - t0


def show(title, results):
    hit = [r for r in results if r[1] != 0]
    line = " ".join("%s:%s" % (r[0], r[1]) for r in results)
    print("  %-30s %s%s" % (title, line, "" if not hit else "   <== 触发!"))
    return bool(hit)


async def main():
    acc = pick()
    if not acc:
        print("没有可用账号")
        return
    print("账号 %s 后端 %s\n" % (acc.get("id"), wx_http.backend_name()))
    hit_any = False

    print("--- timestamp 方向 ---")
    old_ts = str(int((time.time() - 300) * 1000))
    hit_any |= show("T1 timestamp -300s", [await req(acc, "old", timestamp=old_ts)])
    await asyncio.sleep(4)

    fut_ts = str(int((time.time() + 300) * 1000))
    hit_any |= show("T2 timestamp +300s", [await req(acc, "future", timestamp=fut_ts)])
    await asyncio.sleep(4)

    same_ts = str(int(time.time() * 1000))
    r1 = await req(acc, "same1", timestamp=same_ts)
    await asyncio.sleep(1)
    r2 = await req(acc, "same2", timestamp=same_ts)
    hit_any |= show("T3 两次相同 timestamp", [r1, r2])
    await asyncio.sleep(4)

    bad_ts = str(int((time.time() - 86400) * 1000))
    hit_any |= show("T3b timestamp -1天", [await req(acc, "day", timestamp=bad_ts)])
    await asyncio.sleep(4)

    print("\n--- 跨接口并发 ---")
    res = await asyncio.gather(
        req(acc, "I(post_list)"),
        req(acc, "C(post_list)", base=BASE_C, page=PAGE_C),
        req(acc, "I2(post_list)"),
    )
    hit_any |= show("T4 跨 base 并发 x3", res)

    print("\n结论: %s" % ("找到可复现的触发条件" if hit_any
                          else "timestamp / 跨接口并发都不触发 300342"))
    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
