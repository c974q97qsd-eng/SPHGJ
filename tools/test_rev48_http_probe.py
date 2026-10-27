# -*- coding: utf-8 -*-
"""rev48 实测: 纯 HTTP 直播探测能覆盖哪些账号、各花多久。

用母本真实 config 的账号(含 storage_state)直接调 backend.live_probe_http.probe_http,
**全程不启浏览器**。要验证:
  1. 有 cookie + 有规格的账号 -> 拿到 status,判定正确
  2. 没 cookie 的账号 -> 返回 None 且 why=no_cookie(调用方据此暂停并回退)
  3. 整体耗时是否在秒级(这是把探测频率提到 120s 的前提)
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from backend import live_probe_http as lph
from backend import live_fetcher as _lf

BASE = r"\\Sphgj\sphgj"
# 本机不是运行主机,规格缓存在母本 data/ 下。真跑时(母本上)这行不需要,
# 这里只是让本机能读到同一份文件。
_lf._SPEC_CACHE_PATH = os.path.join(BASE, "data", "live_api_spec.json")


def main():
    cfg = json.load(io.open(os.path.join(BASE, "config.json"), encoding="utf-8"))
    accs = cfg.get("accounts") or []
    print("HTTP 后端:", lph.backend_name())
    print("账号数:", len(accs))
    print()

    async def run():
        for a in accs:
            aid = a.get("id")
            diag = {}
            t0 = time.time()
            res = await lph.probe_http(a, diag)
            cost = (time.time() - t0) * 1000
            head = "%-14s %6.0fms " % (aid, cost)
            if res:
                print(head + "-> live=%s reason=%-10s status=%s stream=%s via=%s"
                      % (res["live"], res["reason"], res["status"],
                         res["has_stream"], res["via"]))
            else:
                print(head + "-> None  why=%s %s"
                      % (diag.get("why"), diag.get("code") or diag.get("err") or ""))

    asyncio.run(run())

    # 顺带验证 cookie 头拼装,确认只取 weixin.qq.com 域
    print()
    print("--- cookie 头抽查(2 号)---")
    a2 = next((a for a in accs if (a.get("id") or "").endswith("2")), None)
    if a2:
        jar = lph.build_cookie_header(a2.get("storage_state"))
        names = [p.split("=", 1)[0] for p in jar.split("; ") if p]
        print("条数:", len(names), "名字:", names)
        print("长度: %d 字节(原 storage_state cookies 未过滤时=%d 条)"
              % (len(jar), len((a2.get("storage_state") or {}).get("cookies") or [])))


if __name__ == "__main__":
    main()
