# -*- coding: utf-8 -*-
"""诊断 300342 第二阶段：验证 _log_finder_id（视频号身份）与 cookie 是否必须匹配。

第一阶段的结论（tools/diag_300342.py）：
    A 正确 aid        -> errCode=0
    B 伪造 aid        -> errCode=0     ← _aid 只是埋点参数，无关
    C 空 aid          -> errCode=0
    D 换 pageUrl      -> errCode=0
    E sessionid 篡改  -> errCode=300330

所以 300342 既不是 _aid 问题也不是 cookie 无效。
剩下的头号嫌疑：**body 里的 _log_finder_id 与 cookie 所属视频号不一致**。
这正好对应 03:05:34 的现场 —— uspoloassn2 正在 relogin，
config 里可能还留着上一个身份的 _log_finder_id，而 cookie 已是新的。

对照组：
    F 正确 cookie + 别的账号的 fid
    G 正确 cookie + 空 fid
    H 当前账号 fid 前 8 位（截断）
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


def load():
    cfg = json.load(open(CFG, encoding="utf-8"))
    rows = []
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        ck = wx_http.build_cookie_header(ss)
        if ck and a.get("_aid") and a.get("_log_finder_id"):
            rows.append((a, ck))
    return rows


async def one(acc, aid, fid, label, page_url=PAGE_I):
    url = wx_http.api_url(BASE_I, "post/post_list", aid, page_url)
    body = wx_http.std_body(fid, pageSize=5, onlyUnread=False,
                            needAllCommentCount=True, userpageType=13,
                            forMcn=False, lastBuff="")
    t0 = time.time()
    try:
        j = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=25)
    except Exception as e:
        print("  %-30s 异常 %r" % (label, e))
        return None
    dt = time.time() - t0
    code = j.get("errCode") if isinstance(j, dict) else None
    msg = j.get("errMsg") if isinstance(j, dict) else None
    lst = ((j.get("data") or {}).get("list") or []) if isinstance(j, dict) else []
    print("  %s%-28s %.2fs errCode=%-8s errMsg=%-20s 条数=%d"
          % ("OK " if code == 0 else "!! ", label, dt, code, msg, len(lst)))
    return code


async def main():
    rows = load()
    print("可用账号 %d 个" % len(rows))
    if len(rows) < 2:
        print("需要至少 2 个账号做错配对照")
        return
    acc, _ = rows[0]
    other, _ = rows[1]
    fid = acc["_log_finder_id"]
    aid = acc["_aid"]
    fid_other = other["_log_finder_id"]
    print("主账号 %s fid=%s..." % (acc.get("id"), fid[:14]))
    print("对照账号 %s fid=%s..." % (other.get("id"), fid_other[:14]))

    print("\n--- 对照组 ---")
    await one(acc, aid, fid, "F0 正确 fid（基线）")
    await asyncio.sleep(3)
    await one(acc, aid, fid_other, "F1 换成别的账号的 fid")
    await asyncio.sleep(3)
    await one(acc, aid, "", "F2 空 fid")
    await asyncio.sleep(3)
    await one(acc, aid, fid[:8], "F3 fid 截断前 8 位")
    await asyncio.sleep(3)
    await one(acc, aid, fid[:-4] + "0000", "F4 fid 尾部篡改")

    await wx_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
