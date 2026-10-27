# -*- coding: utf-8 -*-
"""rev56h：重写 verify_step2 的写操作验证段。

为什么要重写：原来直接拿「库里最新一条评论」当回复目标，有两个坑——
  1. 那可能是一条**子回复**（level=2）。回复子回复会生成嵌套层级，
     万一删不掉会真在用户作品下留一条测试内容。
  2. 那条评论所在作品的 reply/list 可能是被服务端拒绝的那批（status_code=4），
     一旦拒绝就找不回自己发的回复 ⇒ **无法自清理**，只能人工收拾。

新逻辑先把「能不能自清理」探明再动手：
  一级评论 + reply_count==0 + 该评论的 reply/list 可正常返回  → 才作为目标。
找不到就整段跳过（宁可少测一项，也不能留脏数据）。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VER = os.path.join(ROOT, "tools", "dyprobe", "verify_step2.py")

NEW = '''async def verify_write(dy, storage, owner):
    """真实回复一条评论 -> 找回回复 id -> 删掉。全程可逆，结束不留痕。

    🚨 只在「探明能自清理」之后才动手：一级评论 + 0 条子回复 + reply/list 可用。
       reply/list 被服务端按作品拒绝的那批，一旦回复就找不回来，绝不能选。
    """
    print("\\n[6] 写操作：回复 -> 找回自己那条回复 -> 删除（自清理）")

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


'''

s = io.open(VER, encoding="utf-8").read()
i = s.find("async def verify_write")
j = s.find('if __name__ == "__main__":')
assert i > 0 and j > i, "锚点定位失败 i=%s j=%s" % (i, j)
s = s[:i] + NEW + s[j:]
io.open(VER, "w", encoding="utf-8", newline="").write(s)

import py_compile  # noqa: E402
py_compile.compile(VER, doraise=True)
t = io.open(VER, encoding="utf-8").read()
checks = [
    ("新 verify_write 已落地", "找到可安全自清理的目标" in t),
    ("保留 rev56g 回归断言", "库里 export_id 全是明文" in t),
    ("旧的目标选取逻辑已删", 'target = items[0]' not in t),
    ("仍然会删除自己发的回复", "denv = await dy.delete_comment" in t),
]
print("=== 自校验 ===")
bad = 0
for n, ok in checks:
    print(("  PASS  " if ok else "  FAIL  ") + n)
    bad += 0 if ok else 1
print("语法 OK verify_step2.py")
print("结果:", "ALL PASS" if not bad else "有失败项")
raise SystemExit(0 if not bad else 1)
