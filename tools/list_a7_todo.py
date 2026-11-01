# -*- coding: utf-8 -*-
"""只读:列出 7 号 10-30 那批(2~3 天前)还没自动评论过的作品。"""
import asyncio
import datetime
import json
import sqlite3
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")
from backend.api_client import WxApiClient   # noqa: E402

ACC = "uspoloassn7"
DB = r"\\Sphgj\sphgj\data\sphgj.db"


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC][0]
    fid = acc["_log_finder_id"]
    api = WxApiClient(None, acc, cfg, None)
    api._throttle = lambda p: asyncio.sleep(0)

    c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
    done = {r[0] for r in c.execute("SELECT export_id FROM auto_commented WHERE finder_id=?", (fid,))}

    vids, last = [], ""
    for _ in range(2):
        r = asyncio.run(api.fetch_video_list(last_buff=last, page_size=100))
        if not isinstance(r, dict) or r.get("__err"):
            print("取列表失败", r)
            return
        d = r.get("data") or {}
        vids.extend(d.get("list") or [])
        last = d.get("lastBuff") or ""
        if not last:
            break

    now = time.time()
    todo = []
    for v in vids:
        try:
            age = (now - int(v.get("createTime") or 0)) / 86400.0
        except Exception:
            continue
        if age <= 7 and v.get("objectId") not in done:
            todo.append((v["objectId"], age, int(v["createTime"])))

    print("需补自动评论: %d 条" % len(todo))
    out = []
    for oid, age, ct in todo:
        print("  %s  %.1f天  %s" % (oid[-24:], age, datetime.datetime.fromtimestamp(ct).strftime("%m-%d %H:%M")))
        out.append(oid)
    json.dump(out, open(r"D:\SPHGJ\_a7_todo.json", "w"), ensure_ascii=False)
    print("已写 _a7_todo.json")


if __name__ == "__main__":
    main()
