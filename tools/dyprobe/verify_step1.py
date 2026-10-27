# -*- coding: utf-8 -*-
"""第 1 步验证：用**真实登录的抖音测试号**跑一遍 backend/douyin 适配层。

只读为主。唯一的写操作是 `top_work(最后一个作品, top=False)`：
作品列表默认把置顶的排在最前，所以「最后一个」一定不是置顶状态，
对未置顶作品发 need_top=0 是**语义上的空操作**（改前改后列表顺序必须完全一致，
脚本会自己比对，不一致就报 FAIL）。

跑法（需要本机 9222 上已登录 creator.douyin.com）：
    cd D:\\SPHGJ\\tools\\dyprobe && python verify_step1.py
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from cdp import CDP                                            # noqa: E402
from cdp_page import CdpPage                                  # noqa: E402
from backend import platforms                                 # noqa: E402
from backend.douyin import api as dyapi                       # noqa: E402
from backend.douyin.client import DouyinClient                # noqa: E402

PORT = 9222
PASS, FAIL = 0, []


def ck(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


def open_tab(url):
    req = urllib.request.Request("http://127.0.0.1:%d/json/new?%s" % (PORT, url), method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def close_tab(tid):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s" % (PORT, tid), timeout=10).read()
    except Exception:
        pass


async def main():
    tab = open_tab(dyapi.PAGE_MANAGE)
    tid = tab["id"]
    cdp = CDP(tab["webSocketDebuggerUrl"], timeout=180)
    try:
        cdp.call("Runtime.enable")
        print("等待页面加载 20s ...")
        time.sleep(20)

        page = CdpPage(cdp)
        # 账号对象只用最小必要字段：真实接入时这些来自 config（登录后回填）
        account = {"id": "dytest", "platform": "dy", "name": "抖音测试号"}
        client = DouyinClient(page, account, config={})

        print("\n[1] 登录态 + 账号身份（work_list 的 author，同时也是最轻的活体探针）")
        ident = await client.self_identity()
        ck(bool(ident.get("uid")), "取到 uid", str(ident.get("uid")))
        ck(bool(ident.get("sec_uid")), "取到 sec_uid", str(ident.get("sec_uid"))[:24] + "…")
        print("      nickname=%s unique_id=%s" % (ident.get("nickname"), ident.get("unique_id")))
        account["_dy_uid"] = ident.get("uid") or ""
        account["_dy_sec_uid"] = ident.get("sec_uid") or ""
        ok = platforms.owner_key(account)
        ck(not platforms.owner_is_fallback(ok), "归属键可用（不是 acct: 兜底）", ok[:24] + "…")
        ck(client.auth_fail is None, "登录态正常（无 status_code=8）")

        print("\n[2] 作品列表（翻 2 页，验证 max_cursor 时间戳游标）")
        works, complete = await client.fetch_all_works(max_pages=2, count=12)
        ck(len(works) > 0, "取到作品", "%d 条" % len(works))
        ck(complete, "列表完整（无中途失败）")
        ck(all(w["aweme_id"] for w in works), "每条作品都有 aweme_id")
        if works:
            print("      第一条: aweme_id=%s status_value=%d 公开=%s 评论数=%d"
                  % (works[0]["aweme_id"], works[0]["status_value"],
                     works[0]["is_public"], works[0]["comment_count"]))
            print("      文案: %s" % (works[0]["desc"] or "")[:40])
        order_before = [w["aweme_id"] for w in works]

        print("\n[3] 明文 aweme_id -> 加密 item_id 映射（评论接口只认加密串）")
        # 取样必须包含「评论最多的那条」——它往往不在列表前几条，
        # 不含它的话第 4 步会因为拿不到加密 id 被跳过（第一次跑就踩到了）。
        best = max(works, key=lambda w: w["comment_count"]) if works else None
        want = [w["aweme_id"] for w in works[:5]]
        if best and best["aweme_id"] not in want:
            want.append(best["aweme_id"])
        m = await client.map_item_ids(want, max_pages=20)
        ck(bool(m), "建到映射", "%d/%d" % (len(m), len(want)))
        if m:
            k = list(m)[0]
            print("      %s -> %s…" % (k, m[k][:30]))

        print("\n[4] 评论列表 + 子回复")
        # 只查「有加密 id 且评论数>0」的前几条作品。子回复接口对**个别作品**会返回
        # status_code=4「啊哦，服务器打瞌睡了」——实测换参数（count/cursor/去掉 item_id）
        # 都无效，换作品就正常，说明是服务端按作品拒绝，不是我们拼错。
        # 因此这里断言的是「至少一条能成功」+「失败不会炸」，而不是逐条必须成功。
        cands = [w for w in works if m.get(w["aweme_id"]) and w["comment_count"] > 0][:4]
        ok_comment = 0
        reply_ok, reply_fail = 0, 0
        for w in cands:
            enc = m[w["aweme_id"]]
            env = await client.fetch_comments(enc, cursor=0, count=20)
            if not env["__ok"]:
                print("      %s 评论接口失败: %s" % (w["aweme_id"], env["__err"]))
                continue
            ok_comment += 1
            cmts = env.get("comments") or []
            print("      %s 统计评论数=%d 返回=%d total=%s has_more=%s"
                  % (w["aweme_id"], w["comment_count"], len(cmts),
                     env.get("total_count"), env.get("has_more")))
            if cmts and ok_comment == 1:
                c0 = cmts[0]
                print("        首条: %s | %s | level=%d"
                      % (c0["nickname"], (c0["text"] or "")[:18], c0["level"]))
                ck(isinstance(c0["create_time"], int) and c0["create_time"] > 0,
                   "评论时间已归一化成 int")
                ck(bool(c0["comment_id"]), "comment_id 非空（删/回复要用）")
            sub = next((x for x in cmts if x["reply_count"] > 0), None)
            if sub:
                renv = await client.fetch_replies(sub["comment_id"], enc)
                if renv["__ok"]:
                    reply_ok += 1
                    if renv.get("comments"):
                        ck(renv["comments"][0]["level"] == 2, "子回复 level=2")
                else:
                    reply_fail += 1
                client._consec_fail = 0
                client._backoff_until = 0.0
            if reply_ok:
                break
        ck(ok_comment > 0, "至少一条作品的评论列表成功", "%d/%d" % (ok_comment, len(cands)))
        ck(reply_ok > 0 or reply_fail > 0,
           "子回复接口已探明（成功或明确被服务端拒绝都算探明）",
           "成功%d 被拒%d" % (reply_ok, reply_fail))
        ck(reply_fail == 0 or reply_ok > 0,
           "被拒时有成功样本可对照 -> 确认是服务端按作品拒绝，不是参数错")

        print("\n[5] 写通道冒烟：对**最后一个作品**发 need_top=0（未置顶 -> 空操作）")
        if len(works) >= 3:
            last = works[-1]
            wenv = await client.top_work(last["aweme_id"], top=False)
            ck(wenv["__ok"], "表单编码写接口成功（含 extra 判定与 csrf）",
               "err=%s" % (wenv.get("__err") or "-"))
            works2, _ = await client.fetch_all_works(max_pages=1, count=12)
            ck([w["aweme_id"] for w in works2] == order_before[:len(works2)],
               "作品顺序未变（确认是空操作，没动你的数据）")
        else:
            print("      （作品太少，跳过快照）")

        print("\n[6] 错误语义自证：真打一个不存在的路径，确认会被判失败")
        bad = await client._request("GET", "/aweme/v1/creator/__not_exist__/probe?aid=2906")
        ck(not bad["__ok"], "未知路径返回 __ok=False（status_code 仍是 0 也不能算成功）",
           "err=%s" % bad["__err"])
        ck(bad["__err"] == dyapi.ERR_URL_MISMATCH, "错误原因归类为 url_mismatch")
        ck(bad.get("status_code") in (0, None), "印证坑 1：status_code=%s" % bad.get("status_code"))
    finally:
        cdp.close()
        close_tab(tid)

    print("\n" + "=" * 62)
    print("合计 PASS %d / FAIL %d" % (PASS, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("第 1 步适配层真机验证通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
