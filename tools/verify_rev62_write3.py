# -*- coding: utf-8 -*-
"""rev62 端到端验证:page=None 时,create_comment 能不能真的发出去(带置顶)。

用的是 uspoloassn7 自己 10-30 发布的作品 + 该账号自己配置的营销文案,
正是自动评论本该发出、却因 no_page 一直没发出去的那类。
这条作品已被标记 seen,机器人不会重复再发,所以不存在重复评论。
"""
import asyncio
import json
import sys

sys.path.insert(0, r"D:\SPHGJ")
from backend.api_client import WxApiClient   # noqa: E402

ACC = "uspoloassn7"
EXPORT = "export/UzFfBgAAxImnADMiV1WGk8zT4DCaPnCzPzDTj-UgHC2NJA37LQ"


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC][0]
    content = acc.get("auto_comment_content") or "点头像，进入直播间领取优惠券下单！"
    api = WxApiClient(None, acc, cfg, None)     # page=None:线上非直播时的真实状态
    api._throttle = lambda p: asyncio.sleep(0)
    print("账号=%s 作品=%s\n文案=%s\npage=%r" % (ACC, EXPORT[-16:], content, api.page))

    r = asyncio.run(api.post_comment(EXPORT, content))
    print("\n发评论 resp =", str(r)[:400])
    data = (r or {}).get("data") or {}
    cmt = data.get("comment") or {}
    cid = (cmt.get("commentId") if isinstance(cmt, dict) else None) or data.get("commentId")
    print("commentId =", cid)
    print("failed() =", api.failed(r))

    if not cid:
        print("\nRESULT FAIL(没拿到 commentId)")
        return 1

    p = asyncio.run(api.pin_comment(EXPORT, cid))
    print("\n置顶 resp =", str(p)[:300], "| failed() =", api.failed(p))
    print("\nRESULT PASS(评论已真实创建 commentId=%s)" % cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
