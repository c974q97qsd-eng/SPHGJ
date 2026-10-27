# -*- coding: utf-8 -*-
"""诊断 300342 第三阶段：是不是「同账号并发 / 短时高频」。

前两阶段已排除：
    _aid 伪造/为空        -> errCode=0
    _pageUrl 换页         -> errCode=0
    _log_finder_id 错配/空/截断 -> errCode=0
    sessionid 篡改        -> errCode=300330（另一种错误）

所以 300342 只剩「服务端侧限流/并发控制」这一种可能。
现场也对得上：03:05:34 同一秒里，评论抓取（post_list）和直播轻量探测
（check_live_status）**两个不同实例同时发出请求** —— 它们各自有自己的
节流计时器，账号维度没有全局限流。

本脚本量化阈值：
    S1 串行 3 次，间隔 2s      -> 基线
    S2 并发 2 个               -> 是否触发
    S3 并发 4 个               -> 是否触发
    S4 串行 8 次，间隔 0.3s    -> 短时高频是否触发
每轮之间静默 6s，只打只读接口。
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


def pick():
    cfg = json.load(open(CFG, encoding="utf-8"))
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        ck = wx_http.build_cookie_header(ss)
        if ck and a.get("_aid") and a.get("_log_finder_id"):
            return a
    return None


async def req(acc, tag):
    url = wx_http.api_url(BASE_I, "post/post_list", acc["_aid"], PAGE_I)
    body = wx_http.std_body(acc["_log_finder_id"], pageSize=5, onlyUnread=False,
                            needAllCommentCount=True, userpageType=13,
                            forMcn=False, lastBuff="")
    t0 = time.time()
    try:
        j = await wx_http.post_json(acc, url, body, page_url=PAGE_I, timeout=25)
        code = j.get("errCode") if isinstance(j, dict) else None
        msg = j.get("errMsg") if isinstance(j, dict) else None
    except Exception as e:
        code, msg = "EXC", repr(e)[:40]
    return tag, code, msg, time.time() - t0


def report(title, results):
    codes = {}
    for tag, code, msg, dt in results:
        codes[code] = codes.get(code, 0) + 1
    hit = [r for r in results if r[1] != 0]
    print("  %-22s %d 次 -> %s%s"
          % (title, len(results),
             " ".join("%s×%d" % (k, v) for k, v in codes.items()),
             "" if not hit else "   !! 触发: " + str(hit[0][1]) + " / " + str(hit[0][2])))
    return bool(hit)


async def main():
    acc = pick()
    if not acc:
        print("没有可用账号")
        return
    print("账号 %s 后端 %s\n" % (acc.get("id"), wx_http.backend_name()))
    any_hit = False

    print("--- 阈值探测 ---")
    # S1 串行 3 次，间隔 2s
    res = []
    for i in range(3):
        res.append(await req(acc, "s1-%d" % i))
        await asyncio.sleep(2)
    any_hit |= report("S1 串行x3 间隔2s", res)
    await asyncio.sleep(6)

    # S2 并发 2
    res = await asyncio.gather(*[req(acc, "s2-%d" % i) for i in range(2)])
    any_hit |= report("S2 并发x2", res)
    await asyncio.sleep(6)

    # S3 并发 4
    res = await asyncio.gather(*[req(acc, "s3-%d" % i) for i in range(4)])
    any_hit |= report("S3 并发x4", res)
    await asyncio.sleep(6)

    # S4 串行 8 次，间隔 0.3s
    res = []
    for i in range(8):
        res.append(await req(acc, "s4-%d" % i))
        await asyncio.sleep(0.3)
    any_hit |= report("S4 串行x8 间隔0.3s", res)

    print("\n结论: %s" % ("并发/高频会触发 300342" if any_hit
                          else "单账号短时间内 3~8 次请求都不会触发 300342"))
    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
