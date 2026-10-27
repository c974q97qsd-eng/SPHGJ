# -*- coding: utf-8 -*-
"""诊断 300342 第五阶段 + 一个新发现的隐患。

新发现（第四阶段 T3）：两次请求用**完全相同的 timestamp**，两次都返回 errCode=-1。
如果属实，这是多实例并发时的真实隐患（api_client 与 live_probe_http 各自建 body，
同一毫秒生成同一个 timestamp 就会互相挤掉）。

本阶段验证：
    R1 相同 timestamp 串行 3 次 -> -1 是否稳定复现
    R2 相同 timestamp 并发 2 个 -> 观察
    R3 timestamp 逐次 +1ms     -> 应全部成功（证明只要不重复就行）
    X1 用已过期账号(6/9/10)的 cookie -> 确认「自然过期」是 300333 而不是 300342
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http  # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
BASE_I = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_I = "https://channels.weixin.qq.com/micro/interaction/comment"


def rows():
    cfg = json.load(open(CFG, encoding="utf-8"))
    out = []
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        if wx_http.build_cookie_header(ss) and a.get("_aid") and a.get("_log_finder_id"):
            out.append(a)
    return out


async def req(acc, tag, ts=None):
    aid, fid = acc["_aid"], acc["_log_finder_id"]
    url = wx_http.api_url(BASE_I, "post/post_list", aid, PAGE_I)
    body = wx_http.std_body(fid, pageSize=3, onlyUnread=False, needAllCommentCount=True,
                            userpageType=13, forMcn=False, lastBuff="")
    if ts is not None:
        body["timestamp"] = ts
    try:
        j = await wx_http.post_json(acc, url, body, page_url=PAGE_I, timeout=25)
        code = j.get("errCode") if isinstance(j, dict) else None
        msg = j.get("errMsg") if isinstance(j, dict) else None
    except Exception as e:
        code, msg = "EXC", repr(e)[:40]
    return tag, code, msg


def show(title, results):
    print("  %-30s %s" % (title, "  ".join("%s:%s(%s)" % (r[0], r[1], r[2]) for r in results)))


async def main():
    accs = rows()
    if not accs:
        print("没有可用账号")
        return
    acc = accs[0]
    print("主账号 %s\n" % acc.get("id"))

    print("--- R1 相同 timestamp 串行 3 次 ---")
    ts = str(int(time.time() * 1000))
    res = []
    for i in range(3):
        res.append(await req(acc, "r1-%d" % i, ts))
        await asyncio.sleep(2)
    show("同一 ts 用 3 次", res)
    await asyncio.sleep(4)

    print("--- R2 相同 timestamp 并发 2 个 ---")
    ts2 = str(int(time.time() * 1000))
    res2 = await asyncio.gather(req(acc, "r2-a", ts2), req(acc, "r2-b", ts2))
    show("并发同 ts", res2)
    await asyncio.sleep(4)

    print("--- R3 timestamp 逐次 +1ms（应全部成功）---")
    base = int(time.time() * 1000)
    res3 = []
    for i in range(3):
        res3.append(await req(acc, "r3-%d" % i, str(base + i)))
        await asyncio.sleep(0.5)
    show("递增 ts", res3)
    await asyncio.sleep(4)

    print("\n--- X1 其它账号当前错误码（区分 300333 / 300342）---")
    for a in accs:
        r = await req(a, a.get("_log_finder_id", "")[-8:])
        print("  %-14s errCode=%-8s %s" % (a.get("id"), r[1], r[2]))
        await asyncio.sleep(2)

    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
