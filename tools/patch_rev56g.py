# -*- coding: utf-8 -*-
"""rev56g：修「子回复入库把加密 item_id 当 export_id 存」的 bug。

真机现象：verify_step2.py --write 报「拿不到该作品的加密 item_id」。
根因不是映射失败（[1b] 覆盖 58/58 全过），而是：
    _fetch_replies() 入库时把参数 `item_id`（**加密串** @jPdq…==）
    当成 export_id 存了，而 export_id 的语义是**明文 aweme_id**。
于是 reply()/delete_comment() 拿着 @jPdq…== 去 map_item_ids 里找，
当然找不到 —— 报错信息("不在列表前几页")还是我自己写的，属于误导性报错。

本补丁：
  1. _fetch_replies 多收一个 aweme_id，入库用明文；加密串只用于发请求。
  2. manager._item_id_of 增加自描述兼容：传进来的已经是加密串（@ 开头）就直接用，
     这样历史脏数据和「上层只有一个 id」的场景都不会再撞墙。
  3. 补单测：子回复的 export_id 必须是明文，不能是加密串。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FET = os.path.join(ROOT, "backend", "douyin", "fetcher.py")
MGR = os.path.join(ROOT, "backend", "douyin", "manager.py")
TEST = os.path.join(ROOT, "tools", "test_rev56b_dy_fetcher.py")


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次 tag=%s" % (os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


# ---- fetcher：调用点 ----
SKIP=1
if 0: patch(FET, [
    ("call", "n2, ok2 = await self._fetch_replies(c, item_id)",
     "n2, ok2 = await self._fetch_replies(c, item_id, aweme_id)"),
    ("sig", '''    async def _fetch_replies(self, comment, item_id):
        """抓一条评论下的子回复。返回 (新增列表, 是否成功)。"""''',
     '''    async def _fetch_replies(self, comment, item_id, aweme_id):
        """抓一条评论下的子回复。返回 (新增列表, 是否成功)。

        🚨 item_id 是**加密串**（只用于发请求）；aweme_id 才是**明文**（用于入库）。
        两者绝不能混：export_id 列的语义是明文 aweme_id，存成加密串以后
        reply()/delete_comment() 再拿它去查映射表必然查不到，
        而且报错会伪装成「拿不到加密 item_id」，极难定位（踩过一次）。
        """'''),
    ("upsert", '''                await self._s(self.storage.upsert_comment, self.owner, item_id,
                              api.to_storage_comment(sub), self.account_id, platform="dy")''',
     '''                await self._s(self.storage.upsert_comment, self.owner, aweme_id,
                              api.to_storage_comment(sub), self.account_id, platform="dy")'''),
])

# ---- manager：_item_id_of 自描述兼容 ----
if 0: patch(MGR, [
    ("itemid", '''    async def _item_id_of(self, aweme_id):
        """明文 aweme_id -> 加密 item_id（写操作必须用加密串）。"""
        if not aweme_id:
            return "", "缺少作品 id（aweme_id）"''',
     '''    async def _item_id_of(self, aweme_id):
        """明文 aweme_id -> 加密 item_id（写操作必须用加密串）。

        加密串自带前缀 `@`，所以「传进来的已经是加密串」可以直接判定并直用，
        不必再查一次映射 —— 既省一枪请求，也兼容历史脏数据
        （rev56g 之前子回复的 export_id 被错存成加密串）。
        """
        if not aweme_id:
            return "", "缺少作品 id（aweme_id）"
        if str(aweme_id).startswith("@"):
            return str(aweme_id), ""'''),
])

# ---- 单测：子回复 export_id 必须是明文 ----
TEST_ANCHOR = '''    print("\\n[11] to_storage_comment 字段名转换（不能丢作者 id）")'''

TEST_NEW = '''    # [10b] 回归：子回复入库的 export_id 必须是**明文 aweme_id**，不能是加密串
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

    print("\\n[11] to_storage_comment 字段名转换（不能丢作者 id）")'''

s = io.open(TEST, encoding="utf-8").read()
assert s.count(TEST_ANCHOR) == 1, "单测锚点命中 %d 次" % s.count(TEST_ANCHOR)
s = s.replace(TEST_ANCHOR, TEST_NEW, 1)
io.open(TEST, "w", encoding="utf-8", newline="").write(s)

# ---- 自校验 ----
f = io.open(FET, encoding="utf-8").read()
m = io.open(MGR, encoding="utf-8").read()
checks = [
    ("fetcher 调用点传 aweme_id", "_fetch_replies(c, item_id, aweme_id)" in f),
    ("fetcher 签名三分量", "async def _fetch_replies(self, comment, item_id, aweme_id):" in f),
    ("fetcher 子回复入库用明文", '''self.owner, aweme_id,
                              api.to_storage_comment(sub)''' in f),
    ("fetcher 不再有 item_id 当 export_id", "self.owner, item_id," not in f),
    ("manager 自描述兼容", 'if str(aweme_id).startswith("@"):' in m),
]
print("=== 自校验 ===")
bad = 0
for n, ok in checks:
    print(("  PASS  " if ok else "  FAIL  ") + n)
    bad += 0 if ok else 1

import py_compile  # noqa: E402
print("=== 语法检查 ===")
for p in (FET, MGR, TEST):
    try:
        py_compile.compile(p, doraise=True)
        print("  OK   " + os.path.basename(p))
    except Exception as e:
        bad += 1
        print("  FAIL " + os.path.basename(p) + " : " + str(e)[:160])

print("\n结果:", "ALL PASS" if not bad else "有 %d 项失败" % bad)
raise SystemExit(0 if not bad else 1)
