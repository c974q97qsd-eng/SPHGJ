# -*- coding: utf-8 -*-
"""诊断 300342 到底是什么错误。

现象（母本 03:05:34，同一秒两条）：
    [api] HTTP 直连不可用(errCode=300342),回退浏览器 path=post/post_list
    [uspoloassn2] HTTP 探测被拒(errCode=300342),改用浏览器路径探测
    ...而当时 uspoloassn2 正在重新扫码（03:05:38 才写完新登录态）

两个不同接口（评论抓取的 post_list、直播探测的 check_live_status）同一秒返回同一个
错误码 —— 这更像「请求上下文不对」而不是「请求太多」。

对照组（全部只读接口 post_list，每步 1 次，间隔 3s）：
    A 正确 _aid            -> 基线，应为 errCode=0
    B 伪造 _aid            -> 若返回 300342 则实锤是 _aid 不匹配
    C 空 _aid              -> 观察
    D 正确 _aid + 换 _pageUrl -> 观察是否与来源页绑定
    E sessionid 篡改 1 位   -> 区分「会话无效」是 300333 还是 300342

只读、低频、单账号，不改任何数据。
"""
import asyncio
import json
import sys
import time
import uuid

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http  # noqa: E402

CFG = r"\\Sphgj\sphgj\config.json"
BASE_I = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_I = "https://channels.weixin.qq.com/micro/interaction/comment"
PAGE_C = "https://channels.weixin.qq.com/platform/post/list"


def pick_account(want=None):
    cfg = json.load(open(CFG, encoding="utf-8"))
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        ck = wx_http.build_cookie_header(ss)
        if not (ck and a.get("_aid") and a.get("_log_finder_id")):
            continue
        if want and a.get("_log_finder_id") != want:
            continue
        return a, ck
    return None, None


async def one(acc, aid, page_url, label, body_fid=None):
    fid = body_fid or acc.get("_log_finder_id")
    url = wx_http.api_url(BASE_I, "post/post_list", aid, page_url)
    body = wx_http.std_body(fid, pageSize=5, onlyUnread=False,
                            needAllCommentCount=True, userpageType=13,
                            forMcn=False, lastBuff="")
    t0 = time.time()
    try:
        j = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=25)
    except Exception as e:
        print("  %-26s 异常 %r" % (label, e))
        return
    dt = time.time() - t0
    code = j.get("errCode") if isinstance(j, dict) else None
    err = j.get("__err") if isinstance(j, dict) else None
    msg = j.get("errMsg") if isinstance(j, dict) else None
    lst = ((j.get("data") or {}).get("list") or []) if isinstance(j, dict) else []
    flag = "OK " if code == 0 else "!! "
    print("  %s%-24s %.2fs errCode=%-8s errMsg=%-22s __err=%s 条数=%d"
          % (flag, label, dt, code, msg, err, len(lst)))


async def main():
    print("HTTP 后端:", wx_http.backend_name())
    acc, ck = pick_account()
    if not acc:
        print("没有可用账号")
        return
    real_aid = acc.get("_aid")
    print("账号: %s  aid=%s...  cookie=%d 字节"
          % (acc.get("id"), real_aid[:8], len(ck)))

    print("\n--- 对照组 ---")
    await one(acc, real_aid, PAGE_I, "A 正确 aid")
    await asyncio.sleep(3)

    await one(acc, str(uuid.uuid4()), PAGE_I, "B 伪造 aid")
    await asyncio.sleep(3)

    await one(acc, "", PAGE_I, "C 空 aid")
    await asyncio.sleep(3)

    await one(acc, real_aid, PAGE_C, "D 正确 aid + 换pageUrl")
    await asyncio.sleep(3)

    # E: 篡改 sessionid 最后一位（只影响这一次请求，不改配置）
    acc_bad = dict(acc)
    ss = json.loads(json.dumps(acc.get("storage_state") or {}))
    for c in ss.get("cookies") or []:
        if c.get("name") == "sessionid" and c.get("value"):
            v = c["value"]
            c["value"] = v[:-1] + ("0" if v[-1] != "0" else "1")
    acc_bad["storage_state"] = ss
    await one(acc_bad, real_aid, PAGE_I, "E sessionid 篡改1位")

    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
