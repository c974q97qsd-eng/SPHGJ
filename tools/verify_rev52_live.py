# -*- coding: utf-8 -*-
"""rev52 真实链路验证:纯 HTTP 分层抓取(不开浏览器)。

只发 post_list 请求;_fetch_comments_for_video 打桩,不发 comment_list、不写线上库。
临时 SQLite 存采样,验证完删掉。
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend.api_client import WxApiClient      # noqa: E402
from backend.comment_fetcher import CommentFetcher  # noqa: E402
from backend.storage import Storage             # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"


async def run_round(acc, cfg, st, round_no, collect_stats):
    api = WxApiClient(None, acc, cfg, st)
    print("  http_available() =", api.http_available())
    f = CommentFetcher(api, st, acc["id"], config=cfg)
    f._round = round_no

    async def fake_fetch(export_id, on_batch=None):
        collect_stats["comment_calls"] += 1
        collect_stats["oids"].append(export_id)
        return 0, [], [], True

    f._fetch_comments_for_video = fake_fetch
    t0 = time.time()
    res = await f.fetch_all()
    dt = time.time() - t0
    return res, dt, f


async def main():
    cfg = json.load(open(CFG, encoding="utf-8"))
    acc = next(a for a in cfg["accounts"] if a["id"] == "uspoloassn2")
    tmp = os.path.join(tempfile.gettempdir(), "sphgj_rev52_live.db")
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    st = Storage(tmp)

    for rnd in (1, 2, 3):
        stats = {"comment_calls": 0, "oids": []}
        print(f"\n===== 第 {rnd} 轮(mode 由 _mode() 决定)=====")
        res, dt, f = await run_round(acc, cfg, st, rnd, stats)
        total, new_n, new_list, deleted, complete = res
        print(f"  返回: 扫描{total} 检查{stats['comment_calls']} 新增{new_n} "
              f"完整={complete} 耗时{dt:.1f}s")
        print(f"  分区: {json.dumps(f._last_zone['counts'], ensure_ascii=False)} "
              f"依据={f._last_zone['basis']}")
        print(f"  热区前 5 个: {f._last_zone['ordered'][:5]}")

    # 验证采样确实落库
    c = st._conn()
    n = c.execute("SELECT COUNT(*) FROM video_activity WHERE finder_id=?",
                  (f.finder_id,)).fetchone()[0]
    days = c.execute("SELECT DISTINCT day FROM video_activity").fetchall()
    c.close()
    print(f"\n采样行数={n} 日期={[d[0] for d in days]}")

    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)


asyncio.run(main())
