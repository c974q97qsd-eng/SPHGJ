# -*- coding: utf-8 -*-
"""rev56e：把 fetch_item_page 的游标键名从 max_cursor 正名为 cursor，
并给「读错字段名」这个真 bug 补回归单测。

为什么要改名：item/list 响应里根本没有 max_cursor 字段，我们在 envelope 里
继续叫 max_cursor 就是把这个错误认知固化下来，下一个改这块代码的人一定会再踩。
键名正名 + 保留 max_cursor 别名（老调用方不炸）。

同时补 4 组回归断言：
  1. 响应只有 cursor（无 max_cursor）时也能翻页 —— 就是这次踩的坑
  2. cursor 为 "0" / 空串时归一成 "" 且不再请求（不能死循环）
  3. 每页只回 19 条也能翻到第 5 页把 91 条全覆盖 —— 复现真机数字
  4. 加密串缓存生效：第二轮零请求，clear_item_cache() 后重新拉
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "backend", "douyin", "client.py")
TEST = os.path.join(ROOT, "tools", "test_rev56_dy_api.py")


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次 tag=%s" % (os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


# ---------------- client.py ----------------
fip_old = '''    async def fetch_item_page(self, cursor="", count=20):
        env = await self._request("GET", api.item_list_url(cursor=cursor, count=count))
        if not env["__ok"]:
            return env
        m, nxt, more = api.item_map_of(env["raw"])
        return {"__ok": True, "map": m, "max_cursor": nxt, "has_more": more,
                "raw": env["raw"]}'''

fip_new = '''    async def fetch_item_page(self, cursor="", count=20):
        """一页 item/list。返回 {"__ok","map","cursor","has_more","raw"}。

        🚨 游标键是 `cursor`（时间戳字符串）；`max_cursor` 只是兼容别名，
        item/list 真机上从来没有这个字段（坑 4）。新代码一律用 cursor。
        """
        env = await self._request("GET", api.item_list_url(cursor=cursor, count=count))
        if not env["__ok"]:
            return env
        m, nxt, more = api.item_map_of(env["raw"])
        return {"__ok": True, "map": m, "cursor": nxt, "max_cursor": nxt,
                "has_more": more, "raw": env["raw"]}'''

patch(CLI, [
    ("fip", fip_old, fip_new),
    ("map-cursor-key", '            nxt = env.get("max_cursor") or ""',
     '            nxt = env.get("cursor") or env.get("max_cursor") or ""'),
])

# ---------------- 回归单测 ----------------
anchor = '''    # 5.13 self_identity'''

new_cases = '''    # 5.12b 回归：item/list 只有 cursor 字段也要能翻页（rev56d 踩过的真 bug）
    c, t = _client([
        {"extra": {}, "item_info_list": [{"item_id": "E1", "item_id_plain": "1"}],
         "cursor": 1725392030000, "has_more": True},
        {"extra": {}, "item_info_list": [{"item_id": "E2", "item_id_plain": "2"}],
         "cursor": 1722244517000, "has_more": False},
    ])
    m = await c.map_item_ids(["1", "2"])
    ck(m == {"1": "E1", "2": "E2"}, "只有 cursor 字段时也能翻页（坑 4 回归）")
    ck(len(t.calls) == 2, "cursor 翻了两页，实际 %d" % len(t.calls))
    ck("cursor=1725392030000" in t.calls[1]["url"],
       "第二页把响应 cursor 原样回传")

    # 5.12c item_map_of 直接测：字段名 + 空值归一
    mp, nxt, more = api.item_map_of({"item_info_list": [
        {"item_id": "E1", "item_id_plain": "1"}], "cursor": "1699999999000",
        "has_more": True})
    ck(mp == {"1": "E1"} and nxt == "1699999999000" and more is True,
       "item_map_of 读 cursor")
    mp, nxt, more = api.item_map_of({"item_info_list": [], "max_cursor": 123})
    ck(nxt == "123", "没有 cursor 时回落到 max_cursor")
    mp, nxt, more = api.item_map_of({"item_info_list": [], "cursor": 0})
    ck(nxt == "", "cursor=0 归一成空串（等价于到头）")
    mp, nxt, more = api.item_map_of({"item_info_list": [], "cursor": ""})
    ck(nxt == "", "cursor 空串保持空串")
    mp, nxt, more = api.item_map_of(None)
    ck(mp == {} and nxt == "" and more is False, "响应不是 dict -> 空结果不抛异常")

    # 5.12d 复现真机数字：每页 19 条、共 91 条，必须全部覆盖
    pages = []
    all_ids = [str(1000 + i) for i in range(91)]
    for k in range(0, 91, 19):
        chunk = all_ids[k:k + 19]
        last = (k + 19) >= 91
        pages.append({"extra": {},
                      "item_info_list": [{"item_id": "E" + x, "item_id_plain": x}
                                         for x in chunk],
                      "cursor": "" if last else str(1700000000000 + k),
                      "has_more": not last})
    c, t = _client(pages)
    m = await c.map_item_ids(all_ids, max_pages=30)
    ck(len(m) == 91, "91 条作品全覆盖（每页 19 条，翻 5 页），实际 %d" % len(m))
    ck(len(t.calls) == 5, "共翻 5 页，实际 %d" % len(t.calls))

    # 5.12e cursor 不前进必须停，不能死循环
    c, t = _client([{"extra": {},
                     "item_info_list": [{"item_id": "E9", "item_id_plain": "9"}],
                     "cursor": "1700000000000", "has_more": True}] * 5)
    m = await c.map_item_ids(["1"])   # 永远找不到 "1"
    ck(len(t.calls) <= 2, "cursor 不前进 -> 立即停，实际 %d 次" % len(t.calls))
    ck(m == {}, "找不到就返回空（调用方跳过，不拿明文硬试）")

    # 5.12f 加密串缓存：第二轮零请求，清缓存后重新拉
    c, t = _client([
        {"extra": {}, "item_info_list": [{"item_id": "E1", "item_id_plain": "1"}],
         "cursor": "", "has_more": False},
        {"extra": {}, "item_info_list": [{"item_id": "E1x", "item_id_plain": "1"}],
         "cursor": "", "has_more": False},
    ])
    m1 = await c.map_item_ids(["1"])
    n1 = len(t.calls)
    m2 = await c.map_item_ids(["1"])
    ck(m1 == m2 == {"1": "E1"} and len(t.calls) == n1,
       "第二轮命中缓存，零请求")
    c.clear_item_cache()
    m3 = await c.map_item_ids(["1"])
    ck(m3 == {"1": "E1x"} and len(t.calls) == n1 + 1,
       "clear_item_cache() 后重新拉（换账号必须清）")

    # 5.13 self_identity'''

s = io.open(TEST, encoding="utf-8").read()
assert s.count(anchor) == 1, "测试锚点命中 %d 次" % s.count(anchor)
s = s.replace(anchor, new_cases, 1)
io.open(TEST, "w", encoding="utf-8", newline="").write(s)

print("补丁完成")
import py_compile  # noqa: E402
for p in (CLI, TEST):
    py_compile.compile(p, doraise=True)
    print("  语法 OK " + os.path.basename(p))
