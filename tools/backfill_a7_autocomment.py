# -*- coding: utf-8 -*-
"""补评论:7 号 10-30 那批因 no_page 漏掉的自动评论(评论 + 置顶)。

跳过验证时已经发过的那条(DTj-UgHC2NJA37LQ,commentId=15045473007095057573)。
每条间隔 6s(和 WRITE_INTERVAL 4~8s 同量级)。失败不重试,只报告。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")
from backend.api_client import WxApiClient   # noqa: E402

ACC = "uspoloassn7"
DONE_ALREADY = {"export/UzFfBgAAxImnADMiV1WGk8zT4DCaPnCzPzDTj-UgHC2NJA37LQ"}


def cid_of(resp):
    data = (resp or {}).get("data") or {}
    cmt = data.get("comment") or {}
    if isinstance(cmt, dict) and cmt.get("commentId"):
        return cmt["commentId"]
    return data.get("commentId")


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC][0]
    content = acc.get("auto_comment_content") or "点头像，进入直播间领取优惠券下单！"
    api = WxApiClient(None, acc, cfg, None)
    api._throttle = lambda p: asyncio.sleep(0)

    todo = [x for x in json.load(open(r"D:\SPHGJ\_a7_todo.json", encoding="utf-8"))
            if x not in DONE_ALREADY]
    print("待补 %d 条,文案=%s\n" % (len(todo), content))

    ok = 0
    for i, oid in enumerate(todo, 1):
        r = asyncio.run(api.post_comment(oid, content))
        cid = cid_of(r)
        if cid:
            p = asyncio.run(api.pin_comment(oid, cid))
            pin_ok = not api.failed(p)
            ok += 1
            print("  %2d/%d %s  评论 OK commentId=%s  置顶=%s"
                  % (i, len(todo), oid[-20:], cid, "OK" if pin_ok else str(p)[:80]))
        else:
            print("  %2d/%d %s  失败 %s" % (i, len(todo), oid[-20:], str(r)[:120]))
        time.sleep(6)

    print("\n补评论完成 %d/%d" % (ok, len(todo)))


if __name__ == "__main__":
    main()
