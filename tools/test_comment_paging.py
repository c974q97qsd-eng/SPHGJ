"""回归:「评论抓取早停」不能被脏快照锁死(2026-09-26)

线上事故:接口某轮把 commentCount 返回成 0(字段缺失 / post_list 失败被
`or 0` 吞掉),这个 0 被写进 video_stats 当快照;此后该视频走「没评论」分支
直接 continue,连 comment_list 都不发 => 好几天「扫描N视频 新增0评论」。
线上实测有 356 个视频因此从未抓到过评论。

本测试锁住四条不变量:
  A. commentCount 字段缺失 => 不写快照、不早停(必须发 comment_list)
  B. 快照 count 与接口一致、但本地从未抓到过评论 => 必须强制抓一次
  C. 快照 count 与接口一致、且本地已有评论 => 才允许早停(省请求)
  D. 接口返回更小的 count(脏值) => 快照不回退(单调性)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import Storage           # noqa: E402
from backend.comment_fetcher import CommentFetcher  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra and not cond else ""))


def make_cmt(cid, content="hello"):
    return {"commentId": cid, "commentNickname": "顾客A", "commentContent": content,
            "commentHeadurl": "", "commentCreatetime": 1790000000 + int(cid[-1] or 0),
            "commentLikeCount": 0, "username": "v2_other_author"}


class FakeAPI:
    """假接口:按脚本返回视频列表与评论。记录 comment_list 被调用的次数。"""

    def __init__(self, videos, comments_by_oid=None):
        self.videos = videos
        self.comments_by_oid = comments_by_oid or {}
        self.comment_calls = []

    async def fetch_video_list(self, last_buff="", only_unread=False):
        if last_buff:
            return {"data": {"list": [], "lastBuff": ""}}
        return {"data": {"list": self.videos, "lastBuff": ""}}

    async def fetch_comments(self, export_id, last_buff="", comment_selection=False):
        if last_buff:
            return {"data": {"comment": [], "lastBuff": ""}}
        self.comment_calls.append(export_id)
        return {"data": {"comment": self.comments_by_oid.get(export_id, []), "lastBuff": ""}}


def new_storage(tmp):
    return Storage(os.path.join(tmp, "t.db"))


def run_fetch(api, st, acc="acc1"):
    f = CommentFetcher(api, st, acc)
    return asyncio.run(f.fetch_all())


# ---------------- A. commentCount 字段缺失 ----------------
def test_missing_field(tmp):
    print("\n[A] commentCount 字段缺失 => 不写快照、必须发 comment_list")
    st = new_storage(tmp)
    oid = "export/MISS1"
    api = FakeAPI([{"objectId": oid}], {oid: [make_cmt("c1")]})   # 无 commentCount 键
    scanned, new, _, _ = run_fetch(api, st)

    check("扫描到 1 个视频", scanned == 1, f"scanned={scanned}")
    check("字段缺失时发出了 comment_list", api.comment_calls == [oid], f"calls={api.comment_calls}")
    check("抓到了 1 条新评论", new == 1, f"new={new}")
    check("未把 0 写进快照(应保持无记录)",
          st.get_video_comment_count("acc1", oid) is None,
          f"cc={st.get_video_comment_count('acc1', oid)}")


# ---------------- B. 脏快照:cc 一致但从未抓到过评论 ----------------
def test_dirty_snapshot_forced(tmp):
    print("\n[B] 快照 cc==接口 cc,但本地从未抓到过评论 => 必须强制抓一次")
    st = new_storage(tmp)
    oid = "export/DIRTY1"
    # 模拟线上脏数据:快照说 3 条,comments 表一条都没有
    st.set_video_comment_count("acc1", oid, 3)
    api = FakeAPI([{"objectId": oid, "commentCount": 3}],
                  {oid: [make_cmt("d1"), make_cmt("d2"), make_cmt("d3")]})
    scanned, new, _, _ = run_fetch(api, st)

    check("脏快照没有早停,发出了 comment_list",
          api.comment_calls == [oid], f"calls={api.comment_calls}")
    check("把缺失的 3 条评论补回来了", new == 3, f"new={new}")
    check("快照仍为 3", st.get_video_comment_count("acc1", oid) == 3)


# ---------------- C. 正常早停 ----------------
def test_early_stop_works(tmp):
    print("\n[C] cc 一致 且 本地已有评论 => 允许早停(省请求)")
    st = new_storage(tmp)
    oid = "export/OK1"
    api1 = FakeAPI([{"objectId": oid, "commentCount": 2}],
                   {oid: [make_cmt("e1"), make_cmt("e2")]})
    run_fetch(api1, st)
    check("首轮真的抓了", api1.comment_calls == [oid])

    api2 = FakeAPI([{"objectId": oid, "commentCount": 2}], {oid: []})   # 数没变
    scanned, new, _, _ = run_fetch(api2, st)
    check("第二轮正确早停(未再调 comment_list)",
          api2.comment_calls == [], f"calls={api2.comment_calls}")
    check("本轮新增 0", new == 0)

    # 评论数涨了 => 必须抓
    api3 = FakeAPI([{"objectId": oid, "commentCount": 3}],
                   {oid: [make_cmt("e1"), make_cmt("e2"), make_cmt("e9")]})
    scanned, new, _, _ = run_fetch(api3, st)
    check("cc 增加后恢复抓取", api3.comment_calls == [oid], f"calls={api3.comment_calls}")
    check("只新增 1 条(去重生效)", new == 1, f"new={new}")


# ---------------- D. 单调性 ----------------
def test_monotonic_snapshot(tmp):
    print("\n[D] 接口回更小的 count(脏值) => 快照不回退")
    st = new_storage(tmp)
    oid = "export/MONO1"
    st.set_video_comment_count("acc1", oid, 5)
    got = st.set_video_comment_count("acc1", oid, 2)
    check("新值 2 < 旧值 5,快照保留 5", got == 5 and st.get_video_comment_count("acc1", oid) == 5,
          f"got={got}")
    got2 = st.set_video_comment_count("acc1", oid, 8)
    check("新值 8 > 旧值 5,正常增长", got2 == 8)
    # 真为 0 也不该把正数打回 0
    got3 = st.set_video_comment_count("acc1", oid, 0)
    check("真为 0 时也不回退到 0", got3 == 8, f"got={got3}")


# ---------------- E. set 的 0 值不再污染(核心回归) ----------------
def test_zero_pollution_fixed(tmp):
    print("\n[E] 回归线上事故:接口连续报 0 也不再锁死")
    st = new_storage(tmp)
    oid = "export/ZERO1"
    # 第一轮:接口报 0(实际是脏值)
    api1 = FakeAPI([{"objectId": oid, "commentCount": 0}], {oid: []})
    run_fetch(api1, st)
    check("首轮 cc=0 未发请求(正常省流)", api1.comment_calls == [])
    check("快照为 0", st.get_video_comment_count("acc1", oid) == 0)

    # 第二轮:接口恢复正确值 4 => 必须抓(修复前会因 prev==0!=4 而抓,但若再次被写 0 就锁死)
    api2 = FakeAPI([{"objectId": oid, "commentCount": 4}],
                   {oid: [make_cmt(f"z{i}") for i in range(4)]})
    scanned, new, _, _ = run_fetch(api2, st)
    check("接口恢复后成功补抓 4 条", new == 4 and api2.comment_calls == [oid],
          f"new={new} calls={api2.comment_calls}")

    # 第三轮:接口抖动又报一次 0 => 单调性保护,快照不回退,且不锁死
    api3 = FakeAPI([{"objectId": oid, "commentCount": 0}], {oid: []})
    run_fetch(api3, st)
    check("脏 0 未把快照打回 0", st.get_video_comment_count("acc1", oid) == 4,
          f"cc={st.get_video_comment_count('acc1', oid)}")

    # 第四轮:cc 回到 4 => 因 has_comments=True 且数一致,允许早停
    api4 = FakeAPI([{"objectId": oid, "commentCount": 4}], {oid: []})
    run_fetch(api4, st)
    check("状态稳定后正确早停", api4.comment_calls == [], f"calls={api4.comment_calls}")


# ---------------- F. 复位脏数据 + 健康度 ----------------
def test_reset_and_health(tmp):
    print("\n[F] reset_stale_video_counts 复位「有快照但从未抓到评论」的视频")
    st = new_storage(tmp)
    con = st._conn()
    # 造 3 个视频:2 个有 cc 但从未抓过,1 个正常有评论
    # 注意本用例【故意不往 posts 插任何行】—— 线上就有这种
    # 「posts 表里一条都没有」的账号,早期版本用 posts 当过滤器会把它整个漏掉。
    for oid, cc in [("export/P1", 5), ("export/P2", 3), ("export/P3", 2), ("export/P4", 0)]:
        con.execute("INSERT OR REPLACE INTO video_stats(account_id,export_id,comment_count,updated_at) "
                    "VALUES(?,?,?,?)", ("acc1", oid, cc, "2026-09-26T00:00:00"))
    # P3 正常:有真实评论
    con.execute("INSERT OR IGNORE INTO comments(account_id,export_id,comment_id,content) "
                "VALUES(?,?,?,?)", ("acc1", "export/P3", "cp3", "hi"))
    con.commit()
    con.close()

    health0 = {h["account_id"]: h for h in st.video_fetch_health()}
    check("健康度识别出 2 个从未抓过的视频(不依赖 posts 表)",
          health0["acc1"]["never_fetched"] == 2, f"{health0}")

    accs, n = st.reset_stale_video_counts()
    check("复位了 2 条脏记录", n == 2, f"n={n} accs={accs}")
    check("正常视频 P3 的快照未被误删",
          st.get_video_comment_count("acc1", "export/P3") == 2)
    check("脏视频 P1 的快照已清空",
          st.get_video_comment_count("acc1", "export/P1") is None)
    check("脏视频 P2 的快照已清空",
          st.get_video_comment_count("acc1", "export/P2") is None)
    check("cc=0 的视频 P4 不复位(接口说的确没评论)",
          st.get_video_comment_count("acc1", "export/P4") == 0)

    health1 = {h["account_id"]: h for h in st.video_fetch_health()}
    check("复位后健康度为 0", health1["acc1"]["never_fetched"] == 0, f"{health1}")


# ---------------- G. account_stats 兜底 ----------------
def test_account_stats_fallback(tmp):
    print("\n[G] account_stats 读失败时返回缓存,不抛异常")
    st = new_storage(tmp)
    st.upsert_comment("acc1", "export/X", make_cmt("s1"))
    first = st.account_stats()
    check("首次读取正常", len(first) == 1 and first[0]["total"] == 1, f"{first}")

    class Boom:
        def execute(self, *a, **k):
            raise Exception("file is not a database")

        def close(self):
            pass

    st._conn = lambda: Boom()
    st._stats_cache = None
    st._stats_cache_ts = 0.0
    try:
        got = st.account_stats()
        check("读失败未抛异常,返回空列表兜底", got == [], f"got={got}")
    except Exception as e:
        check("读失败未抛异常,返回空列表兜底", False, f"raised {e!r}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for i, fn in enumerate([test_missing_field, test_dirty_snapshot_forced,
                                test_early_stop_works, test_monotonic_snapshot,
                                test_zero_pollution_fixed, test_reset_and_health,
                                test_account_stats_fallback]):
            with tempfile.TemporaryDirectory() as t2:
                fn(t2)
    print(f"\n{'=' * 56}")
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
