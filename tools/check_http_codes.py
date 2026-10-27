"""实测各账号 HTTP 直连可用性(只读 post_list,pageSize=1 最小流量)。

目的是核实「6/9/10 号 HTTP 直连长期返回 300333」这条现在是否还成立,
并区分 1/7 号(storage_state 为空,cookie 从没导出过)。

只发读请求,不碰任何写接口;每账号 1 次、间隔 1s。
"""
import asyncio
import json
import sys

sys.path.insert(0, r"D:\SPHGJ")

from backend.api_client import WxApiClient   # noqa: E402
from backend import wx_http                  # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
accs = json.load(open(CFG, encoding="utf-8")).get("accounts") or []
cfg = {"risk_control": {"api_read_via": "http", "api_write_via": "browser",
                        "read_interval": [0.3, 0.6], "write_interval": [0.3, 0.6]}}

ORDER = ["uspoloassn1", "uspoloassn2", "uspoloassn4", "uspoloassn6",
         "uspoloassn7", "uspoloassn8", "uspoloassn9", "uspoloassn10"]

print("%-14s %-6s %-8s %-9s %s" % ("账号", "cookie", "结果", "errCode", "说明"))
print("-" * 80)


async def main():
    for tid in ORDER:
        a = next((x for x in accs if x.get("id") == tid), None)
        if not a:
            continue
        ck = wx_http.cookie_header_of(a) or ""
        nck = ck.count(";") + 1 if ck else 0
        cli = WxApiClient(None, a, cfg, None)
        try:
            r = await cli.fetch_video_list(page_size=1)
        except Exception as e:
            print("%-14s %-6d %-8s %-9s EXC %s" % (tid, nck, "EXC", "-", str(e)[:40]))
            continue
        err = None if not isinstance(r, dict) else (r.get("__err") or r.get("errCode"))
        if err in (None, 0, "0"):
            lst = ((r or {}).get("data") or {}).get("list") or []
            print("%-14s %-6d %-8s %-9s 正常,返回 %d 条" % (tid, nck, "OK", 0, len(lst)))
        else:
            note = {300333: "登录态过期 -> 需重新扫码",
                    300330: "sessionid 无效",
                    300342: "会话正被新登录顶替",
                    -1: "临时错误(可重试)",
                    "no_cookie": "config 无 cookie",
                    }.get(err, str(r)[:50])
            print("%-14s %-6d %-8s %-9s %s" % (tid, nck, "NG", err, note))
        await asyncio.sleep(1.0)


asyncio.run(main())
