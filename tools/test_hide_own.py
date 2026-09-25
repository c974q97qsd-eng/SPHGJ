# -*- coding: utf-8 -*-
"""「隐藏发出的评论」自检(纯存储层,不启动浏览器、不联网)。

背景(2026-09-25 线上问题):开关已打开,评论区里仍能看到自己账号发出的话术评论。
根因:旧判据只认「本工具登记过的 comment_id」(own_comments / auto_commented),
      而大量自己发出的评论根本没登记(在别处发的 / 早期版本发的),
      于是漏出来。线上库实测:5061 条里只隐藏了 713 条。

新判据(并集,见 Storage._own_exclusion):
  1) comment_id 在本工具登记表里(自动评论+置顶 / 自动回复 / 手动回复)
  2) 作者就是本账号自己 —— 接口 raw 里的 username(作者视频号 id)命中本账号身份,
     或昵称命中(旧数据 raw 缺 id 时兜底);两者都没有则无法判定,保持显示,绝不误伤客户。
身份来源:accounts 配置(_log_finder_id / locked_finder_id / name)+ 库内统计(老 id 账号也能认)。

回归锁:参数顺序(account_id 必须排在 id/name 之前)、账号间不串号、阈值保护。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import Storage  # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)


FID_A = "v2_060000231003b20faec8c7e18f1fc6d3cb0cee34b0773daa92bb86ba5749d1746c790f240ebc@finder"
FID_B = "v2_060000231003b20faec8c4e78e1dc0d4ce0ce536b07781a5a1b2c3d4e5f60718293a4b5c6d7e8f9@finder"
NAME_A = "示范店·甲仓"
NAME_B = "示范店·乙仓"


def cmt(cid, nick, content, username="", ts=1700000000):
    return {"commentId": cid, "commentNickname": nick, "commentContent": content,
            "commentHeadurl": "http://x/h.png", "commentCreatetime": str(ts),
            "commentLikeCount": 0, "readFlag": False, "username": username}


def total(st, **kw):
    _items, t = st.query_comments(limit=1, **kw)
    return t


def ids_of(st, **kw):
    out, off = [], 0
    while True:
        page, _ = st.query_comments(limit=200, offset=off, **kw)
        out.extend(x["comment_id"] for x in page)
        if len(page) < 200:
            return out
        off += 200


def main():
    tmp = tempfile.mkdtemp(prefix="hideown_test_")
    db = os.path.join(tmp, "comments.db")
    st = Storage(db)

    print("\n[1] upsert_comment 落 author_id(作者视频号 id)")
    st.upsert_comment("A", "exp1", cmt("c_self1", NAME_A, "点头像，进入直播间领取优惠券下单！", FID_A))
    st.upsert_comment("A", "exp1", cmt("c_cust1", "客户甲", "有40码吗"))
    r = st.query_comments(account_id="A", limit=10)
    check(total(st, account_id="A") == 2, "两条评论入库")
    import sqlite3
    c = sqlite3.connect(db)
    a1 = c.execute("SELECT author_id FROM comments WHERE comment_id='c_self1'").fetchone()[0]
    a2 = c.execute("SELECT author_id FROM comments WHERE comment_id='c_cust1'").fetchone()[0]
    check(a1 == FID_A, "自己的评论 author_id = 接口给的身份")
    check(a2 == "", "客户评论 author_id 为空")

    print("\n[2] 旧库回填:raw 里有 username 但列是空的 -> 补上")
    c.execute("""INSERT INTO comments(account_id,export_id,comment_id,nickname,content,head_url,
                 create_time,like_count,read_flag,replied,author_id,raw,fetched_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              ("A", "exp1", "c_old1", NAME_A, "点头像，进入直播间领取优惠券下单！", "",
               1700000001, 0, 0, 0, None,
               '{"commentId":"c_old1","username":"%s","commentNickname":"%s"}' % (FID_A, NAME_A), ""))
    c.commit()
    n = st._backfill_author_ids()
    c.close()
    c = sqlite3.connect(db)
    a3 = c.execute("SELECT author_id FROM comments WHERE comment_id='c_old1'").fetchone()[0]
    c.close()
    check(n == 1 and a3 == FID_A, "回填 1 行且值正确 (n=%s)" % n)

    print("\n[3] 不带 own_map(hide_own 旧语义)= 只认登记表")
    st.mark_own_comment("A", "c_self1", "manual_reply")       # 登记表收一条
    st.upsert_comment("A", "exp1", cmt("c_cust2", "客户乙", "多少钱", FID_B, ts=1700000002))
    check(total(st, hide_own=True) == 3, "无 own_map 时:只隐藏登记过的 1 条(4-1=3)")

    print("\n[4] 带 own_map(配置身份)-> 自己发的全隐藏、客户评论全保留")
    own_map = {"A": {"ids": {FID_A}, "names": {NAME_A}}}
    left = ids_of(st, hide_own=True, own_map=own_map)
    check("c_self1" not in left, "登记过的自己评论被隐藏")
    check("c_old1" not in left, "author_id 命中的自己评论被隐藏")
    check("c_cust1" in left and "c_cust2" in left, "客户评论全部保留")
    check(len(left) == 2, "隐藏后剩 2 条(共 4 条)")

    print("\n[5] 账号间不串号(参数顺序回归:account_id 必须排在最前)")
    st.upsert_comment("B", "exp2", cmt("b_self", NAME_B, "点头像，进入直播间领取优惠券下单！", FID_B))
    st.upsert_comment("B", "exp2", cmt("b_cust", "客户丙", "有货吗", FID_A))  # 客户偏偏用 A 的身份
    two = {"A": {"ids": {FID_A}, "names": {NAME_A}}, "B": {"ids": {FID_B}, "names": {NAME_B}}}
    leftA = ids_of(st, account_id="A", hide_own=True, own_map=two)
    leftB = ids_of(st, account_id="B", hide_own=True, own_map=two)
    check("c_self1" not in leftA and "c_old1" not in leftA, "A 的自己评论被隐藏")
    check("b_self" not in leftB, "B 的自己评论被隐藏")
    check("b_cust" in leftB, "B 下的客户评论保留(不受 A 身份影响)")
    check(len(leftA) == 2 and len(leftB) == 1, "两账号各自独立判定 (A剩%s/B剩%s)" % (len(leftA), len(leftB)))

    print("\n[6] 昵称兜底:raw 没有作者 id 时靠昵称认自己")
    st.upsert_comment("A", "exp1", cmt("c_noname", NAME_A, "点头像，进入直播间领取优惠券下单！", "", ts=1700000003))
    left = ids_of(st, account_id="A", hide_own=True, own_map=own_map)
    check("c_noname" not in left, "缺作者 id 但昵称==账号名 -> 仍被隐藏")

    print("\n[7] 无特征的老评论必须保留(不误伤)")
    c = sqlite3.connect(db)
    c.execute("""INSERT INTO comments(account_id,export_id,comment_id,nickname,content,head_url,
                 create_time,like_count,read_flag,replied,author_id,raw,fetched_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              ("A", "exp1", "c_anon", "老客户", "看着不错", "", 1700000004, 0, 0, 0, None, "", ""))
    c.commit()
    c.close()
    left = ids_of(st, account_id="A", hide_own=True, own_map=own_map)
    check("c_anon" in left, "作者/昵称都无身份的老评论保留")

    print("\n[8] 库内学习:账号删掉重建(account_id 变了)也能认出旧 id 上的自己评论")
    Z_ID, Z_NAME = "v2_old_account_finder_id@finder", "老店名"
    for i in range(30):
        st.upsert_comment("Z", "expz", cmt("z_self%d" % i, Z_NAME, "点头像，进入直播间领取优惠券下单！",
                                           Z_ID, ts=1700001000 + i))
    for i in range(3):
        st.upsert_comment("Z", "expz", cmt("z_cust%d" % i, "客户%d" % i, "有码吗", "v2_other%d@finder" % i))
    # 白名单 = accounts 里所有账号的身份(Z 是自己,所以 Z 的身份在名单里)
    allow_ids, allow_names = {Z_ID, FID_A, FID_B}, {Z_NAME, NAME_A, NAME_B}
    learned = st.own_identities_from_library(allow_ids=allow_ids, allow_names=allow_names)
    check("Z" in learned, "老账号 Z 被统计出来(%s)" % sorted(learned.get("Z", {}).get("names", [])))
    check(learned.get("Z", {}).get("names") == {Z_NAME}, "学到 Z 的自己昵称 = 老店名")
    check(Z_ID in learned.get("Z", {}).get("ids", set()), "学到 Z 的自己 id")
    lm = {}
    for acc, e in learned.items():
        lm[acc] = {"ids": set(e.get("ids") or []), "names": set(e.get("names") or [])}
    leftZ = ids_of(st, account_id="Z", hide_own=True, own_map=lm)
    check(len(leftZ) == 3, "Z 的自己评论全隐藏,客户 3 条保留 (剩 %d)" % len(leftZ))

    print("\n[9] 白名单保护:刷屏客户不会被误判成「自己」(否则会连他的真实评论一起藏掉)")
    st2 = Storage(os.path.join(tmp, "c2.db"))
    for i in range(2):                      # 只有 2 条,低于 min_count
        st2.upsert_comment("Y", "e", cmt("y_self%d" % i, "小店", "话术", "v2_y@finder"))
    for i in range(40):                     # 某客户狂刷 40 条,占比 95%
        st2.upsert_comment("Y", "e", cmt("y_c%d" % i, "话痨客户", "在吗", "v2_c@finder"))
    l2 = st2.own_identities_from_library(allow_ids={FID_A}, allow_names={NAME_A})
    check("Y" not in l2, "带白名单:刷屏客户/小店样本都不被认定 -> Y 无结论")
    leftY = ids_of(st2, account_id="Y", hide_own=True, own_map={})
    check(len(leftY) == 42, "不带结论时一条都不隐藏(不误伤),全 42 条可见")
    l2b = st2.own_identities_from_library()  # 无白名单:纯统计会认错,故生产必须传白名单
    check("话痨客户" in ((l2b.get("Y") or {}).get("names") or set()),
          "无白名单时刷屏客户确实会被统计成第一名(所以 server 必须传 allow)")

    print("\n[10] 分页/total 与 items 一致(隐藏后)")
    # 到此 A 下:自己的 = c_self1(登记) / c_old1(author_id) / c_noname(昵称) / c_self9
    #           客户的 = c_cust1 / c_cust2 / c_anon   -> 隐藏后剩 3 条
    page, t = st.query_comments(account_id="A", limit=2, offset=0, hide_own=True, own_map=two)
    check(len(page) == 2 and t == 3, "A 隐藏后 total=3, 首页 2 条 (total=%s)" % t)
    page2, _ = st.query_comments(account_id="A", limit=2, offset=2, hide_own=True, own_map=two)
    check(len(page2) == 1, "第二页 1 条,分页与 total 一致")

    print("\n[11] 搜索/已回筛选与隐藏条件可叠加")
    st.upsert_comment("A", "exp1", cmt("c_self9", NAME_A, "点头像，进入直播间领取优惠券下单！", FID_A, ts=1700000005))
    hits, ht = st.query_comments(account_id="A", q="点头像", hide_own=True, own_map=own_map, limit=50)
    check(ht == 0 and not hits, "搜索命中自己评论时也被隐藏(q=点头像 -> 0)")
    hits2, ht2 = st.query_comments(account_id="A", q="客户", hide_own=True, own_map=own_map, limit=50)
    check(ht2 >= 1, "搜索客户评论仍可查到 (%d)" % ht2)

    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
    print("RESULT", "ALL PASS" if not FAIL else "HAS FAILURE")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
