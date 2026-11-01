# -*- coding: utf-8 -*-
"""只读:7 号当前作品列表里,「7 天闸门」放行多少条 = 修好后一轮可能补评论的上限。

不写任何东西:只发 post_list 读请求 + 查本地库的 auto_commented。
"""
import asyncio
import datetime
import json
import sqlite3
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")
from backend.api_client import WxApiClient   # noqa: E402

ACC_ID = "uspoloassn7"
DB = r"\\Sphgj\sphgj\data\sphgj.db"


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC_ID][0]
    fid = acc.get("_log_finder_id")
    api = WxApiClient(None, acc, cfg, None)
    api._throttle = lambda p: asyncio.sleep(0)

    c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
    done = {r[0] for r in c.execute("SELECT export_id FROM auto_commented WHERE finder_id=?", (fid,))}
    print("本地已自动评论过:", len(done), "条")

    vids = []
    last = ""
    for _ in range(4):                     # 最多 4 页 × 100
        r = asyncio.run(api.fetch_video_list(last_buff=last, page_size=100))
        if not isinstance(r, dict) or r.get("__err") or r.get("errCode") not in (0, None):
            print("  取列表失败:", str(r)[:120])
            break
        d = r.get("data") or {}
        lst = d.get("list") or []
        vids.extend(lst)
        last = d.get("lastBuff") or ""
        if not lst or not last:
            break

    now = time.time()
    print("取到作品:", len(vids))
    buckets = {"<=1天": 0, "2-3天": 0, "4-7天": 0, "8-30天": 0, ">30天": 0}
    day_burst = {"<=1天": 0, "2-3天": 0, "4-7天": 0}
    for v in vids:
        try:
            age = (now - int(v.get("createTime") or 0)) / 86400.0
        except Exception:
            continue
        k = ("<=1天" if age <= 1 else "2-3天" if age <= 3 else
             "4-7天" if age <= 7 else "8-30天" if age <= 30 else ">30天")
        buckets[k] += 1
        if k in day_burst and v.get("objectId") not in done:
            day_burst[k] += 1

    print("\n按作品年龄分布:", buckets)
    print("其中【还没自动评论过】的:", day_burst, "=> 7 天闸门下会补评论", sum(day_burst.values()), "条")
    oldest = min((int(v.get("createTime") or 0) for v in vids), default=0)
    newest = max((int(v.get("createTime") or 0) for v in vids), default=0)
    for k, t in (("最新", newest), ("最老", oldest)):
        if t:
            print("  %s作品: %s" % (k, datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")))


if __name__ == "__main__":
    main()
