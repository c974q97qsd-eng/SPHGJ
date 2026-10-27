"""直接走 wx_http 打 post_list,不经 api_client 的回退逻辑,拿到原始错误码。

用 pageSize=1 最小流量,每账号 1 次、间隔 1s,纯读。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http   # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
accs = json.load(open(CFG, encoding="utf-8")).get("accounts") or []
ORDER = ["uspoloassn1", "uspoloassn2", "uspoloassn4", "uspoloassn6",
         "uspoloassn7", "uspoloassn8", "uspoloassn9", "uspoloassn10"]

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE = "https://channels.weixin.qq.com/platform/post/list"

print("%-14s %-6s %-9s %s" % ("账号", "cookie", "errCode", "说明"))
print("-" * 70)


async def main():
    for tid in ORDER:
        a = next((x for x in accs if x.get("id") == tid), None)
        if not a:
            continue
        ck = wx_http.cookie_header_of(a) or ""
        nck = ck.count(";") + 1 if ck else 0
        if nck == 0:
            print("%-14s %-6d %-9s %s" % (tid, 0, "-", "config 里没有可用 cookie(从未导出)"))
            continue
        aid = (a.get("_aid") or "").strip()
        from urllib.parse import quote
        url = ("%s/post/post_list?_aid=%s&_pageUrl=%s"
               % (BASE, aid, quote(PAGE, safe="")))
        body = {"_aid": aid, "_log_finder_id": a.get("_log_finder_id") or "",
                "timestamp": str(int(time.time() * 1000)),
                "pageSize": 1, "onlyUnread": False, "needAllCommentCount": True,
                "userpageType": 13, "forMcn": False, "lastBuff": ""}
        try:
            r = await wx_http.post_json(a, url, body, page_url=PAGE, timeout=20)
        except Exception as e:
            print("%-14s %-6d %-9s EXC %s" % (tid, nck, "-", str(e)[:40]))
            continue
        err = r.get("__err") if isinstance(r, dict) else "?"
        code = r.get("errCode") if isinstance(r, dict) else None
        if err:
            print("%-14s %-6d %-9s __err=%s" % (tid, nck, "-", err))
        else:
            note = {0: "正常"}.get(code, "异常")
            n = len(((r.get("data") or {}).get("list")) or [])
            print("%-14s %-6d %-9s %s(list=%d)" % (tid, nck, code, note, n))
        await asyncio.sleep(1.0)


asyncio.run(main())
