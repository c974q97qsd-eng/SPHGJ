# -*- coding: utf-8 -*-
"""rev62 真机验证:page=None(浏览器已收回)时,写操作能不能真发出去。

两步:
  1) 读:post_list 走 HTTP —— 顺带确认 7 号的 cookie 还活着
  2) 写:pin_comment 用**无效**的 exportId/commentId 探一次 —— 只为验证
     「请求真的出了本机、服务端真的回了包」,不会碰到任何真实作品/评论。
     判据:响应里不能出现 no_page(修复前就是它)。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend.api_client import WxApiClient   # noqa: E402

ACC_ID = "uspoloassn7"


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC_ID][0]
    api = WxApiClient(None, acc, cfg, None)     # ← 关键:page 为 None,等价线上「浏览器已收回」
    api._throttle = lambda p: asyncio.sleep(0)

    print("账号 %s | page=%r | _aid=%s | cookies=%d" % (
        ACC_ID, api.page, (acc.get("_aid") or "")[:12],
        len((acc.get("storage_state") or {}).get("cookies") or [])))

    print("\n---- 1) 读:post_list(HTTP) ----")
    r = asyncio.run(api.fetch_video_list(page_size=3))
    if isinstance(r, dict):
        lst = ((r.get("data") or {}).get("list") or [])
        print("  errCode =", r.get("errCode"), "| __err =", r.get("__err"), "| 返回条数 =", len(lst))
        for v in lst[:3]:
            ct = v.get("createTime")
            try:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ct)))
            except Exception:
                ts = str(ct)
            print("    createTime =", ct, "->", ts, "| objectId =", str(v.get("objectId"))[:40])
        ok_read = (r.get("__err") is None and r.get("errCode") in (0, None))
    else:
        ok_read = False
    print("  读结论:", "OK(cookie 活着,HTTP 可用)" if ok_read else "失败")

    print("\n---- 2) 写:pin_comment(无效 id,零副作用) ----")
    w = asyncio.run(api.pin_comment("export/REV62PROBE_INVALID", "0"))
    print("  resp =", str(w)[:300])
    err = str((w or {}).get("__err") or "")
    no_page = "no_page" in err
    reached = not no_page
    print("  写结论:", "请求已发出并拿到服务端响应(无 no_page)" if reached else "仍是 no_page —— 修复未生效")

    print("\nRESULT", "PASS" if (ok_read and reached) else "FAIL")
    return 0 if (ok_read and reached) else 1


if __name__ == "__main__":
    raise SystemExit(main())
