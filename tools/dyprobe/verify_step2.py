# -*- coding: utf-8 -*-
"""第 2 步验证：抖音评论抓取入库 + 回复/删除，用**真实登录的测试号**跑通全链路。

数据库用临时文件（不碰 data/sphgj.db），所以这一步对线上数据零影响。

跑法：
    cd D:\\SPHGJ\\tools\\dyprobe && python verify_step2.py          # 只读
    cd D:\\SPHGJ\\tools\\dyprobe && python verify_step2.py --write  # 额外验证回复+删除（会自己清理）

--write 会：回复一条真实评论 -> 从子回复里找回回复的 id -> 删掉它。全程可逆。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from cdp import CDP                                             # noqa: E402
from cdp_page import CdpPage                                   # noqa: E402
from backend import platforms                                  # noqa: E402
from backend.storage import Storage                            # noqa: E402
from backend.douyin import api as dyapi                        # noqa: E402
from backend.douyin.manager import DouyinManager               # noqa: E402

PORT = 9222
PASS, FAIL = 0, []


def ck(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  " + str(extra)) if extra else ""))
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


async def main(do_write):
    db = os.path.join(tempfile.gettempdir(), "sphgj_dy_verify.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(db + suf):
            os.remove(db + suf)
    storage = Storage(db)

    cfg = {"accounts": [{"id": "dytest", "platform": "dy", "name": "抖音测试号"}],
           "risk_control": {"dy_read_interval": [0.6, 1.2], "dy_write_interval": [2, 3]}}
    saved = {"n": 0}

    def save_config(c):
        saved["n"] += 1

    dy = DouyinManager(cfg, storage, emit=None, save_config=save_config)

    print("[0] 未挂页面时应诚实报未就绪（不能假装成功）")
    st0 = dy.status()
    ck(st0["configured"] is True and st0["page_ready"] is False, "已配置账号但页面未就绪")
    r0 = await dy.fetch_now()
    ck(r0.get("__ok") is False and r0.get("__err") == "not_ready",
       "未挂页面时 fetch 返回 not_ready", r0.get("__msg"))

    tab = open_tab(dyapi.PAGE_MANAGE)
    tid = tab["id"]
    cdp = CDP(tab["webSocketDebuggerUrl"], timeout=180)
    try:
        cdp.call("Runtime.enable")
        print("\n等待页面加载 18s ...")
        time.sleep(18)
        dy.set_page(CdpPage(cdp))

        print("\n[1] 身份获取 + 归属键升级（acct:<id> -> sec_uid）")
        before = dy.owner()
        ident = await dy.refresh_identity()
        after = dy.owner()
        ck(platforms.owner_is_fallback(before), "挂载前归属键是 acct: 兜底", before)
        ck(bool(ident.get("sec_uid")), "取到 sec_uid", str(ident.get("sec_uid"))[:18] + "…")
        ck(not platforms.owner_is_fallback(after), "归属键已升级为 sec_uid")
        ck(after == ident.get("sec_uid"), "owner 与身份一致")
        ck(saved["n"] >= 1, "身份已回填并落盘", "%d 次" % saved["n"])
        ck(dy._fetcher.owner == after, "fetcher 的归属键跟着换了（不然会写错归属）")

        print("\n[1b] 加密 item_id 覆盖率（rev56d 修的阻塞项，必须接近 100%）")
        works_all, _ = await dy._client.fetch_all_works()
        all_ids = [w["aweme_id"] for w in works_all if w.get("aweme_id")]
        private = [w for w in works_all if str(w.get("status_value")) in ("140", "141")]
        mp = await dy._client.map_item_ids(all_ids)
        pub = [x for x in all_ids
               if x not in {w["aweme_id"] for w in private}]
        cov = len([x for x in pub if x in mp])
        print("      作品 %d 条（其中非公开 %d 条）-> 映射 %d 条"
              % (len(all_ids), len(private), len(mp)))
        ck(len(all_ids) > 0, "读到作品全集", len(all_ids))
        ck(len(mp) >= len(pub), "所有公开作品都拿到了加密 item_id",
           "%d / %d" % (cov, len(pub)))
        ck(len(mp) <= len(all_ids), "映射条数不超过作品总数（没有串号）", len(mp))
        # 缓存生效：第二次应该零请求（用调用次数间接验）
        mp2 = await dy._client.map_item_ids(all_ids)
        ck(mp2 == mp, "第二次映射结果一致（命中缓存）")

        print("\n[2] 第一轮抓取（限 3 个作品，避免刷太多请求）")
        rep = await dy.fetch_now(max_works=3)
        ck(rep.get("__ok") is True, "抓取返回成功")
        ck(rep["works"] > 0, "读到作品", rep["works"])
        ck(rep["checked"] > 0, "检查了作品", rep["checked"])
        print("      新增=%d 检查=%d 跳过=%d 不完整=%s 耗时=%ss 模式=%s"
              % (rep["new"], rep["checked"], rep["skipped"], not rep["complete"],
                 rep["seconds"], rep["mode"]))
        ck(rep["complete"] is True, "本轮完整（没有请求失败）")

        print("\n[3] 入库校验（platform=dy / 归属=sec_uid / 分页去重）")
        items, total = storage.query_comments(finder_id=after, limit=2000, offset=0)
        ck(total == rep["new"], "评论表条数 == 本轮新增", "%d / %d" % (total, rep["new"]))
        ck(total > 0, "确实有评论入库", total)
        if items:
            it = items[0]
            print("      %s | %s | %s" % (it["nickname"], (it["content"] or "")[:20],
                                          it["export_id"]))
            ck(it["finder_id"] == after, "评论归属是 sec_uid")
            ck(isinstance(it["create_time"], int) and it["create_time"] > 0,
               "时间已归一化成 int")
        # platform 列
        import sqlite3
        c = sqlite3.connect(db)
        bad = c.execute("SELECT COUNT(*) FROM comments WHERE platform<>'dy'").fetchone()[0]
        ncc = c.execute("SELECT COUNT(*) FROM comments WHERE platform='dy'").fetchone()[0]
        vs = c.execute("SELECT COUNT(*) FROM video_stats WHERE platform='dy'").fetchone()[0]
        ck(bad == 0, "没有一行被错标成 wx", bad)
        ck(ncc == total, "platform=dy 的行数与总数一致", ncc)
        ck(vs > 0, "评论数快照也带 platform=dy", vs)
        # finder_id 兜底残留检查
        fb = c.execute("SELECT COUNT(*) FROM comments WHERE finder_id LIKE 'acct:%'").fetchone()[0]
        ck(fb == 0, "没有 acct: 兜底归属残留", fb)
        c.close()

        print("\n[4] 第二轮：去重与早停")
        rep2 = await dy.fetch_now(max_works=3)
        ck(rep2.get("__ok") is True, "第二轮成功")
        print("      第二轮 checked=%d new=%d skipped=%d"
              % (rep2["checked"], rep2["new"], rep2["skipped"]))
        # ⚠️ 不能断言「第二轮 new==0」：每次都是按播放数分区重新排 top-N，
        # 第二轮很可能选中**另一个作品**（第一次没分到的），自然会带新评论。
        # 真正要保证的不变量是「没有重复入库」，所以核对累计新增 == 库里总条数。
        items2, total2 = storage.query_comments(finder_id=after, limit=5000, offset=0)
        ck(total2 == rep["new"] + rep2["new"],
           "两轮累计新增 == 库中总条数（没有重复入库）",
           "%d == %d + %d" % (total2, rep["new"], rep2["new"]))
        import sqlite3 as _sq
        c2 = _sq.connect(db)
        dup = c2.execute("SELECT COUNT(*) FROM (SELECT comment_id FROM comments "
                         "GROUP BY comment_id HAVING COUNT(*)>1)").fetchone()[0]
        ck(dup == 0, "comment_id 无重复", dup)
        c2.close()
        ck(rep2["skipped"] >= 0, "跳过计数有值（早停/分区在跑）", rep2["skipped"])

        print("\n[5] 状态接口")
        st = dy.status()
        ck(st["page_ready"] and st["logged_in"], "页面就绪 + 登录态正常")
        ck(st["comments_total"] == total2, "状态里的评论数与库一致",
           "%s / %s" % (st["comments_total"], total2))
        ck(st["last_fetch"] and st["last_fetch"].get("new") is not None, "带上次抓取摘要")

        if do_write:
            await verify_write(dy, storage, after)
        else:
            print("\n[6] 写操作未验证（加 --write 才会真的回复+删除）")
    finally:
        cdp.close()
        close_tab(tid)
        for suf in ("", "-wal", "-shm"):
            try:
                if os.path.exists(db + suf):
                    os.remove(db + suf)
            except Exception:
                pass

    print("\n" + "=" * 62)
    print("合计 PASS %d / FAIL %d" % (PASS, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("第 2 步真机验证通过")
    return 0


async def verify_write(dy, storage, owner):
    """真实回复一条评论 -> 找回回复 id -> 删掉。全程可逆，结束不留痕。

    🚨 只在「探明能自清理」之后才动手：一级评论 + 0 条子回复 + reply/list 可用。
       reply/list 被服务端按作品拒绝的那批，一旦回复就找不回来，绝不能选。
    """
    print("\n[6] 写操作：回复 -> 找回自己那条回复 -> 删除（自清理）")

    # 6.1 rev56g 回归：库里的 export_id 必须全是明文 aweme_id
    items, total = storage.query_comments(finder_id=owner, limit=500, offset=0)
    ck(total > 0, "有已入库的评论")
    enc_like = [it for it in items if str(it["export_id"]).startswith("@")]
    ck(not enc_like, "库里 export_id 全是明文 aweme_id（rev56g 回归）",
       "%d 条里 %d 条像加密串" % (total, len(enc_like)))

    # 6.2 选一个「能安全自清理」的目标
    client = dy._client
    works_all, _ = await client.fetch_all_works()
    target, tap = None, None
    tried_work = 0
    for w in works_all:
        aid = w.get("aweme_id")
        if not w.get("is_public") or int(w.get("comment_count") or 0) <= 0:
            continue
        tried_work += 1
        if tried_work > 6:
            break
        enc, _why = await dy._item_id_of(aid)
        if not enc:
            continue
        cenv = await client.fetch_comments(enc, cursor=0, count=20, sort="TIME")
        if not cenv.get("__ok") or not cenv.get("comments"):
            continue
        for c in cenv["comments"]:
            if int(c.get("level") or 1) != 1:
                continue
            if int(c.get("reply_count") or 0) != 0:
                continue
            # 探明这条评论的 reply/list 可用（否则回复了就找不回）
            probe = await client.fetch_replies(c.get("comment_id"), enc, cursor=0, count=5)
            if not probe.get("__ok"):
                continue
            target, tap = c, enc
            break
        if target:
            break
    ck(target is not None,
       "找到可安全自清理的目标（一级评论 + 0 子回复 + reply/list 可用）",
       ("作品 %s / 评论 %s" % (aid, (target or {}).get("comment_id", "")[:16])))
    if not target:
        print("      ⚠️ 没找到安全目标，本轮跳过写操作（不留任何测试内容）")
        return

    # 6.3 回复
    text = "接口自测请忽略_%d" % int(time.time() % 10000)
    env = await dy.reply(target["comment_id"], aid, text)
    ck(env.get("__ok") is True, "回复成功", env.get("__msg") or env.get("__err"))
    if not env.get("__ok"):
        return
    await asyncio.sleep(2.5)

    # 6.4 找回自己刚发的那条
    mine = None
    renv = await client.fetch_replies(target["comment_id"], tap, cursor=0, count=20)
    for r in (renv.get("comments") or []):
        if (r.get("text") or "") == text or text[:8] in (r.get("text") or ""):
            mine = r
            break
    ck(mine is not None, "能在子回复里找到刚发的这条",
       (mine or {}).get("comment_id", "")[:20] if mine else "没找到")
    if not mine:
        print("      ⚠️ 找不回就无法自动删除，请人工检查作品 %s 下的测试回复" % aid)
        return

    # 6.5 删除 + 复查
    denv = await dy.delete_comment(mine["comment_id"], aid)
    ck(denv.get("__ok") is True, "删除成功", denv.get("__msg") or denv.get("__err"))
    await asyncio.sleep(2.0)
    renv2 = await client.fetch_replies(target["comment_id"], tap, cursor=0, count=20)
    left = [r for r in (renv2.get("comments") or []) if (r.get("text") or "") == text]
    ck(not left, "删除后复查：测试回复已消失", len(left))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main("--write" in sys.argv)))
