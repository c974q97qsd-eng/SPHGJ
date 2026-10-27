# -*- coding: utf-8 -*-
"""rev56b 抖音评论抓取引擎离线单测 —— 不联网、不开浏览器、不需要 playwright。

重点钉死「用血换来的四条规矩」（见 backend/douyin/fetcher.py 顶部）：
  1. commentCount 缺失 ≠ 没有评论（不写快照、不跳过）
  2. 「评论数没变」早停的前提是本地真抓到过评论（has_comments）
  3. 请求失败必须算不完整，不能当成功（抖音 status_code=0 也可能是失败）
  4. 子回复被服务端按作品拒绝（status_code=4）要降级跳过，不能判整轮失败

跑法: python tools/test_rev56b_dy_fetcher.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.douyin import api
from backend.douyin.fetcher import DouyinCommentFetcher

PASS, FAIL = 0, []


def ck(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  " + str(extra)) if extra else ""))
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


# ---------------- 假客户端 ----------------
class FakeClient:
    def __init__(self, works=None, mapping=None, comments=None, replies=None,
                 works_ok=True, comments_ok=True, reply_ok=True, cursor_stuck=False):
        self._works = works or []
        self._mapping = mapping or {}
        self._comments = comments or {}
        self._replies = replies or {}
        self.works_ok = works_ok
        self.comments_ok = comments_ok
        self.reply_ok = reply_ok
        self.cursor_stuck = cursor_stuck
        self.calls = []
        self._consec_fail = 0
        self._backoff_until = 0.0

    async def fetch_all_works(self, max_pages=40, count=12, status=0):
        self.calls.append(("works",))
        if not self.works_ok:
            return [], False
        return list(self._works), True

    async def map_item_ids(self, aweme_ids, max_pages=20, count=20):
        self.calls.append(("map", tuple(aweme_ids)))
        return {k: v for k, v in self._mapping.items() if k in set(aweme_ids or [])}

    async def fetch_comments(self, item_id, cursor=0, count=20, sort="TIME"):
        self.calls.append(("comments", item_id, cursor))
        if not self.comments_ok:
            return {"__ok": False, "__err": api.ERR_BAD_PARAMS}
        pages = self._comments.get(item_id) or []
        idx = 0 if not self.cursor_stuck else 0
        if cursor >= len(pages):
            return {"__ok": True, "comments": [], "cursor": cursor, "has_more": False,
                    "total_count": 0}
        kw = dict(pages[cursor]) if isinstance(pages, dict) else pages[cursor]
        if self.cursor_stuck:
            kw["cursor"] = cursor      # 游标不前进
        return dict({"__ok": True}, **kw)

    async def fetch_replies(self, comment_id, item_id, cursor=0, count=20):
        self.calls.append(("replies", comment_id))
        if not self.reply_ok:
            return {"__ok": False, "__err": "status_4", "__msg": api.explain("status_4")}
        return {"__ok": True, "comments": self._replies.get(comment_id) or [],
                "cursor": 0, "has_more": False, "total_count": 0}

    def write_blocked(self):
        return False


class FakeStorage:
    def __init__(self, state=None, existing=None):
        # state: {aweme_id: {"cc":..,"has_comments":bool,"seen":bool}}
        self.state = dict(state or {})
        self.existing = set(existing or [])
        self.upserts = []       # (owner, aweme_id, platform, cid)
        self.snapshots = []     # (aweme_id, count, platform)
        self.activity = []
        self.baseline = {}

    def get_video_fetch_state(self, finder_id, export_id):
        s = self.state.get(export_id)
        if not s:
            return dict(seen=False, cc=None, has_comments=False)
        return dict(seen=True, cc=s.get("cc"), has_comments=bool(s.get("has_comments")))

    def set_video_comment_count(self, finder_id, export_id, count, account_id=None, platform="wx"):
        self.snapshots.append((export_id, count, platform))
        return count

    def is_comment_exists(self, cid):
        return cid in self.existing

    def upsert_comment(self, finder_id, export_id, comment, account_id=None, platform="wx"):
        self.upserts.append((finder_id, export_id, platform, comment.get("commentId")))
        self.existing.add(comment.get("commentId"))

    def upsert_video_activity(self, finder_id, items, account_id=None, platform="wx"):
        self.activity.append((finder_id, len(items), platform))
        return len(items)

    def get_activity_baseline(self, finder_id, days=3):
        return dict(self.baseline)


def work(aid, cc, read=1000, status=102):
    return {"aweme_id": aid, "desc": "d", "comment_count": cc, "read_count": read,
            "like_count": 1, "status_value": status, "is_public": status == 102}


def comment(cid, reply=0, level=1):
    return {"comment_id": cid, "text": "t", "create_time": 1788000000,
            "like_count": 1, "reply_count": reply, "level": level,
            "is_author": False, "nickname": "n", "author_id": "u", "head_url": "",
            "raw": {"comment_id": cid}}


def fetcher(client, storage, cfg=None):
    f = DouyinCommentFetcher(client, storage, "dyA", "SEC",
                             config=cfg or {"scan_cold_every": 0, "scan_normal_every": 0})
    return f


# ---------------- 用例 ----------------
async def t_basic():
    print("\n[1] 基本抓取：作品 -> 映射 -> 评论 -> 入库(platform=dy)")
    cli = FakeClient(
        works=[work("A", 2)],
        mapping={"A": "ENCA"},
        comments={"ENCA": [{"comment_info_list": None, "comments": [
            {"comment_id": "c1", "text": "x", "create_time": 1, "like_count": 0,
             "reply_count": 0, "level": 1, "nickname": "n", "author_id": "u", "head_url": "",
             "raw": {}},
            {"comment_id": "c2", "text": "y", "create_time": 2, "like_count": 0,
             "reply_count": 0, "level": 1, "nickname": "n", "author_id": "u", "head_url": "",
             "raw": {}},
        ], "cursor": 1, "has_more": False, "total_count": 2}]},
    )
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["new"] == 2, "新增 2 条评论", rep["new"])
    ck(rep["complete"] is True, "complete=True")
    ck(all(u[2] == "dy" for u in st.upserts), "入库 platform 全是 dy")
    ck(all(u[0] == "SEC" for u in st.upserts), "归属键用的是 sec_uid（不是 account_id）")
    ck(("A", 2, "dy") in st.snapshots, "写了评论数快照且 platform=dy")
    ck((("works",)) in cli.calls, "调用了作品列表")
    ck(rep["zone"]["basis"] in ("read", "order", "growth"), "分区依据有值", rep["zone"])


async def t_early_stop():
    print("\n[2] 早停：评论数没变 + 本地已有评论 -> 不发评论请求")
    cli = FakeClient(works=[work("A", 5)], mapping={"A": "ENCA"},
                     comments={"ENCA": []})
    st = FakeStorage(state={"A": {"cc": 5, "has_comments": True, "seen": True}})
    rep = await fetcher(cli, st).fetch_all()
    ck(not any(c[0] == "comments" for c in cli.calls), "一个评论请求都没发")
    ck(rep["skipped"] == 1, "计入 skipped", rep["skipped"])
    ck(rep["checked"] == 0, "checked=0")

    print("  -- 反例：has_comments=False（历史脏 0）必须强制抓")
    cli2 = FakeClient(works=[work("A", 5)], mapping={"A": "ENCA"},
                      comments={"ENCA": [{"comments": [], "cursor": 1, "has_more": False}]})
    st2 = FakeStorage(state={"A": {"cc": 5, "has_comments": False, "seen": True}})
    rep2 = await fetcher(cli2, st2).fetch_all()
    ck(any(c[0] == "comments" for c in cli2.calls), "has_comments=False -> 仍然抓（不永久锁死）")
    ck(rep2["checked"] == 1, "checked=1")


async def t_missing_cc():
    print("\n[3] commentCount 字段缺失 -> 不写快照、不跳过、照样抓")
    w = work("A", None)
    cli = FakeClient(works=[w], mapping={"A": "ENCA"},
                     comments={"ENCA": [{"comments": [comment("c1")], "cursor": 1,
                                         "has_more": False}]})
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["checked"] == 1, "仍然检查该作品")
    ck(not st.snapshots, "没有写评论数快照（缺失不等于 0）", st.snapshots)
    ck(rep["new"] == 1, "评论照常入库")


async def t_zero_cc():
    print("\n[4] 接口明确说 0 评论 -> 记快照后跳过（下一轮不再请求）")
    cli = FakeClient(works=[work("A", 0)], mapping={"A": "ENCA"})
    st = FakeStorage(state={"A": {"cc": 0, "has_comments": False, "seen": True}})
    rep = await fetcher(cli, st).fetch_all()
    ck(not any(c[0] == "comments" for c in cli.calls), "0 评论作品不发评论请求")
    ck(("A", 0, "dy") in st.snapshots, "把 0 写进快照")


async def t_reply_degrade():
    print("\n[5] 子回复被服务端拒绝(status_code=4) -> 降级跳过，不算整轮失败")
    cli = FakeClient(works=[work("A", 1)], mapping={"A": "ENCA"}, reply_ok=False,
                     comments={"ENCA": [{"comments": [comment("c1", reply=1)],
                                         "cursor": 1, "has_more": False}]})
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["complete"] is True, "整轮仍算完整（子回复是可降级项）")
    ck(rep["reply_rejected"] == 1, "记了 1 组被拒", rep.get("reply_rejected"))
    ck(len([u for u in st.upserts if u[3] == "c1"]) == 1, "一级评论照样入库")

    print("  -- 正常情况：子回复入库且 level=2")
    cli2 = FakeClient(works=[work("A", 1)], mapping={"A": "ENCA"},
                      comments={"ENCA": [{"comments": [comment("c1", reply=1)],
                                          "cursor": 1, "has_more": False}]},
                      replies={"c1": [comment("s1", level=2)]})
    st2 = FakeStorage()
    rep2 = await fetcher(cli2, st2).fetch_all()
    ck(rep2["new"] == 2, "一级 1 + 子回复 1 = 2", rep2["new"])
    ck(any(u[3] == "s1" for u in st2.upserts), "子回复已入库")


async def t_comment_fail():
    print("\n[6] 评论接口失败 -> complete=False（不能静默当成功）")
    cli = FakeClient(works=[work("A", 3)], mapping={"A": "ENCA"}, comments_ok=False)
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["complete"] is False, "complete=False")
    ck(not st.snapshots, "失败时**不写快照**（否则下轮会按脏值跳过）", st.snapshots)
    ck(rep["new"] == 0, "新增 0")


async def t_works_fail():
    print("\n[7] 作品列表失败 -> complete=False 且不发后续请求")
    cli = FakeClient(works_ok=False)
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["complete"] is False, "complete=False")
    ck(rep["works"] == 0, "works=0")
    ck(not any(c[0] == "comments" for c in cli.calls), "没有发评论请求")


async def t_unmapped():
    print("\n[8] 拿不到加密 item_id 的作品 -> 跳过，不拿明文硬试")
    cli = FakeClient(works=[work("A", 2)], mapping={})   # 故意没有映射
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(not any(c[0] == "comments" for c in cli.calls), "没有用明文 aweme_id 发评论请求")
    ck(rep["unmapped"] == 1, "计入 unmapped", rep["unmapped"])
    ck(rep["checked"] == 0, "checked=0")


async def t_cursor_stuck():
    print("\n[9] cursor 不前进 -> 立即停（防死循环）")
    pages = [{"comments": [comment("c1")], "cursor": 0, "has_more": True}]
    cli = FakeClient(works=[work("A", 9)], mapping={"A": "ENCA"},
                     comments={"ENCA": pages}, cursor_stuck=True)
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    n_comments = len([c for c in cli.calls if c[0] == "comments"])
    ck(n_comments <= 2, "评论请求次数 <= 2（没死循环），实际 %d" % n_comments)
    ck(rep["new"] == 1, "不重复计新增")


async def t_pagination():
    print("\n[10] 评论分页：两页都抓，cursor 递进")
    pages = [{"comments": [comment("c1")], "cursor": 1, "has_more": True},
             {"comments": [comment("c2")], "cursor": 2, "has_more": False}]
    cli = FakeClient(works=[work("A", 2)], mapping={"A": "ENCA"},
                     comments={"ENCA": pages})
    st = FakeStorage()
    rep = await fetcher(cli, st).fetch_all()
    ck(rep["new"] == 2, "两页共 2 条", rep["new"])
    cursors = [c[2] for c in cli.calls if c[0] == "comments"]
    ck(cursors == [0, 1], "cursor 依次 0 -> 1", cursors)


async def t_storage_mapping():
    # [10b] 回归：子回复入库的 export_id 必须是**明文 aweme_id**，不能是加密串
    #       （rev56g 修的真 bug：混用会让回复/删除报「拿不到加密 item_id」）
    from backend.douyin.client import DouyinClient  # noqa: F401

    class _SubClient:
        """父评论 1 条带 1 条子回复。"""
        auth_fail = None

        def __init__(self):
            self.item_ids = []

        async def fetch_all_works(self, max_pages=40, count=12):
            return [work("W1", 1)], True

        async def map_item_ids(self, ids, max_pages=30, count=20, use_cache=True):
            return {str(x): "ENC_" + str(x) for x in ids}

        async def fetch_comments(self, item_id, cursor=0, count=20, sort="TIME"):
            self.item_ids.append(item_id)
            return {"__ok": True, "comments": [comment("C1", reply=1)],
                    "cursor": 0, "has_more": False, "total_count": 1}

        async def fetch_replies(self, comment_id, item_id, cursor=0, count=20):
            return {"__ok": True, "comments": [comment("S1", level=2)],
                    "cursor": 0, "has_more": False, "total_count": 1}

    st = FakeStorage()
    cl = _SubClient()
    rep = await fetcher(cl, st).fetch_all()
    sub_rows = [u for u in st.upserts if u[3] == "S1"]
    ck(len(sub_rows) == 1, "子回复已入库")
    ck(sub_rows[0][1] == "W1", "子回复的 export_id 是明文 aweme_id（不是加密串）",
       sub_rows[0][1])
    parent_rows = [u for u in st.upserts if u[3] == "C1"]
    ck(parent_rows and parent_rows[0][1] == "W1", "父评论的 export_id 同样是明文")
    ck(all(u[1] == "W1" for u in st.upserts), "本次入库的 export_id 全部是明文")
    ck(all(not str(u[1]).startswith("@") for u in st.upserts),
       "入库里没有 @ 开头的加密串")
    ck("ENC_W1" in cl.item_ids, "发请求时用的是加密串（两者没混）")

    print("\n[11] to_storage_comment 字段名转换（不能丢作者 id）")
    row = api.to_storage_comment(comment("c1"))
    ck(row["commentId"] == "c1", "commentId")
    ck(row["commentContent"] == "t", "commentContent")
    ck(row["username"] == "u", "username（识别自己发的评论要用）")
    ck(isinstance(row["commentCreatetime"], int) and row["commentCreatetime"] > 0,
       "时间来成 int")
    ck("_dy" in row, "原始抖音字段留档在 _dy")


async def main():
    for fn in (t_basic, t_early_stop, t_missing_cc, t_zero_cc, t_reply_degrade,
               t_comment_fail, t_works_fail, t_unmapped, t_cursor_stuck,
               t_pagination, t_storage_mapping):
        await fn()
    print("\n" + "=" * 60)
    print("合计 PASS %d / FAIL %d" % (PASS, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
