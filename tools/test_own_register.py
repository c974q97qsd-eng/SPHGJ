# -*- coding: utf-8 -*-
"""「本工具发出的评论」登记链路自检(纯存储层 + 假 API,不启动浏览器、不联网)。

背景(2026-09-25 线上核查):
  own_comments 表长期为 0 行 —— 不是"没发过评论",而是登记链路本身残缺:
   1) 自动评论只写 auto_commented(主键 account_id+export_id,同一作品多次评论会被
      INSERT OR REPLACE 覆盖,旧 id 丢失),从不写 own_comments;
   2) 发评论/回复成功但接口没回 commentId 时,只记一个空串(AUTO_COMMENTED.comment_id=''),
      那条评论永久失联 —— 线上有 13 条这样的实例(2026-09-09/09-10);
   3) 没有任何事后补登机制。

修好后的链路:
  发出 -> 拿到 id: mark_own_comment(own_comments,via=auto_comment/auto_reply/manual_reply)
       -> 没拿到: mark_own_comment_pending(记 export_id+内容+来源)
  抓到评论后(comment_fetcher 每轮结束) reconcile_own_comments:
       按 export_id(有则限定)+ 内容逐字相同,把自己发的评论补上真实 comment_id。

本测试锁住:补登正确、不误登记客户评论、不跨作品/跨账号串、幂等、清理超龄、三处调用点都登记。
"""
import asyncio
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import Storage          # noqa: E402
from backend.auto_comment import AutoCommenter  # noqa: E402
from backend.auto_reply import AutoReply     # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)


FID_A = "v2_060000231003b20faec8c7e18f1fc6d3cb0cee34b0773daa92bb86ba5749d1746c790f240ebc@finder"
FID_B = "v2_060000231003b20faec8c4e78e1dc0d4ce0ce536b07781a5a1b2c3d4e5f60718293a4b5c6d7e8f9@finder"
TALK = "点头像，进入直播间领取优惠券下单！"
TALK2 = "点头像，来直播间有活动价格~"


def cmt(cid, nick, content, username="", ts=1700000000):
    return {"commentId": cid, "commentNickname": nick, "commentContent": content,
            "commentHeadurl": "http://x/h.png", "commentCreatetime": str(ts),
            "commentLikeCount": 0, "readFlag": False, "username": username}


class FakeApi:
    """按预设响应返回;记录调用,便于断言。"""

    def __init__(self, post_resp=None, reply_resp=None):
        self.post_resp = post_resp if post_resp is not None else {}
        self.reply_resp = reply_resp if reply_resp is not None else {}
        self.calls = []

    async def post_comment(self, export_id, content):
        self.calls.append(("post", export_id, content))
        return self.post_resp

    async def pin_comment(self, export_id, comment_id):
        self.calls.append(("pin", export_id, comment_id))
        return {"data": {}}

    async def reply_comment(self, cid, text):
        self.calls.append(("reply", cid, text))
        return self.reply_resp


def own_ids(st, account_id=None):
    c = sqlite3.connect(st.db_path)
    try:
        if account_id:
            return {r[0] for r in c.execute("SELECT comment_id FROM own_comments WHERE account_id=?", (account_id,))}
        return {r[0] for r in c.execute("SELECT comment_id FROM own_comments")}
    finally:
        c.close()


def pend_rows(st):
    c = sqlite3.connect(st.db_path)
    try:
        return c.execute("SELECT account_id,export_id,content,via FROM own_comment_pending").fetchall()
    finally:
        c.close()


def hide_ids(st, account_id="A", own_map=None):
    out, off = [], 0
    while True:
        page, _ = st.query_comments(account_id=account_id, limit=200, offset=off,
                                    hide_own=True, own_map=own_map or {})
        out.extend(x["comment_id"] for x in page)
        if len(page) < 200:
            return out
        off += 200


def main():
    tmp = tempfile.mkdtemp(prefix="ownreg_test_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    st = Storage(os.path.join(tmp, "comments.db"))

    print("\n[1] mark_own_comment:登记本工具发出的评论(带来源)")
    st.mark_own_comment("A", "c_own1", "auto_comment")
    check(own_ids(st, "A") == {"c_own1"}, "登记成功")
    row = sqlite3.connect(st.db_path).execute(
        "SELECT account_id, via FROM own_comments WHERE comment_id='c_own1'").fetchone()
    check(row == ("A", "auto_comment"), "账号与来源记录正确 %r" % (row,))
    st.mark_own_comment("A", "", "auto_comment")
    check(own_ids(st, "A") == {"c_own1"}, "空 comment_id 不写入")
    st.mark_own_comment("A", "c_own1", "manual_reply")
    check(len(own_ids(st, "A")) == 1, "同一 id 重复登记不产生重复行")

    print("\n[2] 待确认登记:pending 写入与去重")
    st.mark_own_comment_pending("A", "exp1", TALK, "auto_comment")
    st.mark_own_comment_pending("A", "exp1", TALK, "auto_comment")
    check(len(pend_rows(st)) == 1, "同(账号,作品,内容,来源)只留一条,不因重试堆积")
    check(st.pending_own_comment_count("A") == 1, "计数接口可用")
    check(st.pending_own_comment_count("B") == 0, "按账号计数正确")
    st.mark_own_comment_pending("A", "exp1", TALK2, "auto_comment")
    check(len(pend_rows(st)) == 2, "不同内容各自留一条")

    print("\n[3] 拿到真实 id 后,待确认项自动清掉(不会重复回填)")
    st.mark_own_comment("A", "c_own_talk", "auto_comment", content=TALK, export_id="exp1")
    left = [r for r in pend_rows(st) if r[2] == TALK]
    check(not left, "内容匹配的待确认项被清除")
    check(len(pend_rows(st)) == 1, "不相关内容仍保留")

    print("\n[4] reconcile:按 export_id + 内容补登真实 comment_id")
    st.upsert_comment("A", "exp1", cmt("c_real_new", "店名", TALK2, FID_A))
    n = st.reconcile_own_comments("A")
    check(n == 1, "补登 1 条(返回 %d)" % n)
    check("c_real_new" in own_ids(st, "A"), "补登到 own_comments")
    via = sqlite3.connect(st.db_path).execute(
        "SELECT via FROM own_comments WHERE comment_id='c_real_new'").fetchone()[0]
    check(via == "auto_comment", "来源沿用待确认记录的 via=%s" % via)
    check(st.pending_own_comment_count("A") == 0, "补登后待确认清空")

    print("\n[5] reconcile 幂等:重复跑不会再补/不会报错")
    check(st.reconcile_own_comments("A") == 0, "第二次补登 0 条")

    print("\n[6] 不误登记客户评论(内容不同 -> 不匹配)")
    st.mark_own_comment_pending("A", "exp1", "我们的暗号是888", "auto_reply")
    st.upsert_comment("A", "exp1", cmt("c_cust", "客户甲", "我发的不是暗号", "v2_cust@finder"))
    n = st.reconcile_own_comments("A")
    check(n == 0, "内容不同 -> 不补登")
    check("c_cust" not in own_ids(st, "A"), "客户评论未被误登记")
    check(st.pending_own_comment_count("A") == 1, "待确认保留,等下一轮")

    print("\n[7] 不跨作品串:同内容但不同 export_id 不匹配")
    st.upsert_comment("A", "exp_other", cmt("c_other_exp", "店名", "我们的暗号是888", FID_A))
    n = st.reconcile_own_comments("A")
    check(n == 0, "作品不同 -> 不补登(有 export_id 时必须一致)")
    check("c_other_exp" not in own_ids(st, "A"), "别的作品那条没被误登记")

    print("\n[8] 待确认没带 export_id 时,退化为按内容匹配")
    st.mark_own_comment_pending("A", "", "跨作品兜底话术", "manual_reply")
    st.upsert_comment("A", "exp_x", cmt("c_noexp", "店名", "跨作品兜底话术", FID_A))
    n = st.reconcile_own_comments("A")
    check(n == 1, "只有无 export_id 的那条补上(带 export_id=exp1 的仍不匹配 exp_x)")
    check("c_noexp" in own_ids(st, "A"), "跨作品兜底那条已登记")
    check(st.pending_own_comment_count("A") == 1, "带 export_id 的仍留在待确认")

    print("\n[9] 不跨账号串:A 的待确认不会登记到 B 的评论")
    st2 = Storage(os.path.join(tmp, "acc.db"))
    st2.mark_own_comment_pending("A", "e1", "甲店话术", "auto_comment")
    st2.upsert_comment("B", "e1", cmt("c_b_same", "乙店", "甲店话术", FID_B))
    check(st2.reconcile_own_comments("A") == 0, "B 的评论不会补到 A 名下")
    check(own_ids(st2, "A") == set(), "A 名下仍为空")
    st2.upsert_comment("A", "e1", cmt("c_a_same", "甲店", "甲店话术", FID_A))
    check(st2.reconcile_own_comments("A") == 1, "A 自己那条才补登")
    check(own_ids(st2, "A") == {"c_a_same"}, "补登的是自己的评论")

    print("\n[10] 超龄待确认被清理(避免无限堆积)")
    st2.mark_own_comment_pending("A", "e9", "永远匹配不到的话术", "auto_comment")
    c = sqlite3.connect(st2.db_path)
    c.execute("UPDATE own_comment_pending SET created_at='2000-01-01T00:00:00' WHERE content='永远匹配不到的话术'")
    c.commit()
    c.close()
    st2.reconcile_own_comments("A", max_age_days=30)
    check(st2.pending_own_comment_count("A") == 0, "过期未匹配的待确认被丢弃")

    print("\n[11] 集成·自动评论:接口回 commentId -> 直接登记 own_comments")
    st3 = Storage(os.path.join(tmp, "ac.db"))
    acc = {"id": "A", "auto_comment_enabled": True, "auto_comment_content": TALK}
    api = FakeApi(post_resp={"data": {"comment": {"commentId": "cid_9001"}}})
    ac = AutoCommenter(api, st3, acc)
    ok = asyncio.run(ac.try_comment("expA"))
    check(ok, "发了评论")
    check("cid_9001" in own_ids(st3, "A"), "own_comments 拿到登记(不再只写 auto_commented)")
    check(st3.is_auto_commented("A", "expA"), "auto_commented 仍记录(避免重发)")
    check(st3.pending_own_comment_count("A") == 0, "有 id 时不产生待确认")

    print("\n[12] 集成·自动评论:接口没回 commentId -> 写待确认,抓到后补登")
    api2 = FakeApi(post_resp={"data": {}})
    ac2 = AutoCommenter(api2, st3, acc)
    asyncio.run(ac2.try_comment("expB"))
    check("expB" in (r[1] for r in pend_rows(st3)), "记下待确认(内容+作品)")
    check(str(st3.is_auto_commented("A", "expB")) == "True", "仍标记已发,避免重试连发")
    st3.upsert_comment("A", "expB", cmt("cid_9002", "店名", TALK, FID_A))
    check(st3.reconcile_own_comments("A") == 1, "抓到后补登成功")
    check("cid_9002" in own_ids(st3, "A"), "那条评论现在有真实 id 了")

    print("\n[13] 集成·自动回复:两种响应都要登记")
    st4 = Storage(os.path.join(tmp, "ar.db"))
    cfg = {"enabled": True, "rules": [{"keyword": "38码", "reply": "38码有货,请私信"}]}
    st4.upsert_comment("A", "expR", cmt("c_q1", "客户", "有38码吗", "v2_cust@finder"))
    api3 = FakeApi(reply_resp={"data": {"comment": {"commentId": "rid_1"}}})
    ar = AutoReply(api3, st4, "A", cfg)
    asyncio.run(ar.reply_comment({"commentId": "c_q1", "commentContent": "有38码吗"}, "expR"))
    check("rid_1" in own_ids(st4, "A"), "回复拿到 id -> 登记")
    v = sqlite3.connect(st4.db_path).execute(
        "SELECT via FROM own_comments WHERE comment_id='rid_1'").fetchone()[0]
    check(v == "auto_reply", "来源 = auto_reply")
    st4.upsert_comment("A", "expR", cmt("c_q2", "客户", "40码呢", "v2_cust2@finder"))
    api4 = FakeApi(reply_resp={"data": {}})
    cfg2 = {"enabled": True, "rules": [{"keyword": "40码", "reply": "40码也有"}]}
    ar2 = AutoReply(api4, st4, "A", cfg2)
    asyncio.run(ar2.reply_comment({"commentId": "c_q2", "commentContent": "40码呢"}, "expR"))
    check("40码也有" in (r[2] for r in pend_rows(st4)), "没拿到 id -> 记待确认")
    st4.upsert_comment("A", "expR", cmt("rid_2", "店名", "40码也有", FID_A))
    check(st4.reconcile_own_comments("A") == 1, "补登成功")
    check("rid_2" in own_ids(st4, "A"), "rid_2 已登记")

    print("\n[14] 「隐藏发出的评论」能藏住补登过的评论")
    own_map = {"A": {"ids": set(), "names": set()}}   # 故意不给身份,只靠登记表
    left = hide_ids(st4, "A", own_map)
    check("rid_1" not in left and "rid_2" not in left, "两条自己发的都被藏住")
    check("c_q1" in left and "c_q2" in left, "客户两条仍显示")

    print("\n[15] comment_fetcher 每轮结束会触发补登(不联网,空作品列表)")
    from backend.comment_fetcher import CommentFetcher

    class EmptyApi:
        def __init__(self):
            self.n = 0

        async def fetch_video_list(self, last_buff="", only_unread=False):
            self.n += 1
            return {"data": {"list": [], "lastBuff": ""}}

    st5 = Storage(os.path.join(tmp, "cf.db"))
    st5.mark_own_comment_pending("A", "expF", "待补登话术", "auto_comment")
    st5.upsert_comment("A", "expF", cmt("cid_f1", "店名", "待补登话术", FID_A))
    cf = CommentFetcher(EmptyApi(), st5, "A")
    res = asyncio.run(cf.fetch_all())
    check("cid_f1" in own_ids(st5, "A"), "fetch_all 结束后自动补登完成")
    check(st5.pending_own_comment_count("A") == 0, "待确认已清空")
    check(len(res) == 4, "fetch_all 返回值结构不变(4 元组)")

    print("\n" + "=" * 62)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
