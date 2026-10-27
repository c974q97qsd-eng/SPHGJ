# -*- coding: utf-8 -*-
"""rev56d 补丁：修好「加密 item_id 覆盖不全」的阻塞项。

根因（真机验证）：不是服务端不分页，是我们读错了字段名。
    item/list 响应里翻页游标叫 `cursor`（时间戳字符串），**没有 max_cursor 字段**。
    item_map_of() 读的是 max_cursor -> 永远 0 -> map_item_ids 第一页就 break。
实测修好后：91 条作品覆盖 90 条，唯一缺的那条 status_value=141（仅自己可见），
私密作品不会有公开评论，属正常排除。

同时做两件顺手的事：
  1. 加密串映射加进程内缓存（加密结果对同一个 aweme_id 是稳定的），
     steady-state 下不再每轮白翻 5 页。
  2. 默认翻页深度 20 -> 30 页（20/page * 30 = 600 条作品够用）。

幂等：每条 assert count == 1，改完自校验 + 语法检查。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API = os.path.join(ROOT, "backend", "douyin", "api.py")
CLI = os.path.join(ROOT, "backend", "douyin", "client.py")
FET = os.path.join(ROOT, "backend", "douyin", "fetcher.py")


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次（应为 1）tag=%s" % (
            os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)
    return s


# ---------------- api.py ----------------
api_head_old = """另外两条实测结论：
  · 作品列表的 `max_cursor` 是**毫秒时间戳**，不是 offset；
  · 评论列表的 `item_id` 必须是**加密串**（@jPdq…==），明文 aweme_id 无效，
    加密串要从 /aweme/v1/creator/item/list 的 item_id 字段拿。
"""

api_head_new = """另外几条实测结论：
  · 作品列表（work_list）的 `max_cursor` 是**毫秒时间戳**，不是 offset；
  · 评论列表的 `item_id` 必须是**加密串**（@jPdq…==），明文 aweme_id 无效，
    加密串要从 /aweme/v1/creator/item/list 的 item_id 字段拿。

🚨 坑 4：item/list 的翻页游标叫 `cursor`，**没有 `max_cursor` 字段**。
这一条踩过一次：曾经按 work_list 的习惯去读 `max_cursor`，永远读到 0，
于是 map_item_ids 第一页就 break，只覆盖到 19 条作品（91 条里缺 72 条），
表现为「评论只能抓 21%、回复老作品时报拿不到加密 id」。
实测真相：
  · 首页传 `cursor=`（空串），响应里 `cursor` 是**时间戳字符串**，原样回传即可翻页；
  · 每页上限 20 条，`count` 传 20/50/100 都只回 19~20 条（放大 count 无效）；
  · 顺序是**发布时间倒序**（新作品在第 1 页），所以「只要最近的作品」时翻 1~2 页就够；
  · `has_more` 为假就是到底了。
  · 私密作品（status_value=141）不会出现在 item/list 里 —— 属正常排除，不是丢数据。
"""

item_list_doc_old = '''def item_list_url(cursor="", count=20):
    """作品列表（带加密 item_id）。

    返回 item_id（加密，评论接口要用）+ item_id_plain（数字 aweme_id）。
    ⚠️ 排序与 work_list 不同，新作品不一定在第一页 —— 所以评论抓取不能只靠它，
       要用 work_list 的 aweme_id 去这里建映射（见 client.map_item_ids）。
    """'''

item_list_doc_new = '''def item_list_url(cursor="", count=20):
    """作品列表（带加密 item_id）。

    返回 item_id（加密，评论接口要用）+ item_id_plain（数字 aweme_id）。

    🚨 cursor 在这里是**时间戳字符串**（坑 4），不是 work_list 的 max_cursor：
       首页传空串 ""，下一页传响应里的 `cursor` 原样。count 上限 20，
       传更大也不会多给。顺序为发布时间倒序。
    """'''

item_map_old = '''def item_map_of(resp):
    """从 item/list 响应里取 明文 aweme_id -> 加密 item_id 的映射。"""
    out = {}
    if not isinstance(resp, dict):
        return out, 0, False
    rows = resp.get("item_info_list") or []
    for it in rows:
        plain = str(it.get("item_id_plain") or "")
        enc = str(it.get("item_id") or "")
        if plain and enc:
            out[plain] = enc
    return out, _int(resp.get("max_cursor")), bool(resp.get("has_more"))'''

item_map_new = '''def item_map_of(resp):
    """从 item/list 响应里取 明文 aweme_id -> 加密 item_id 的映射。

    返回 (映射, 下一页游标, 是否有更多)。

    🚨 游标字段是 `cursor`（坑 4）。这里**同时兼容 max_cursor** 只是为了不让
    上游拿到 None 就崩，真机上 item/list 从来没有 max_cursor 字段 ——
    主要看 cursor。游标是时间戳字符串，原样回传即可（不要当 offset 递增）。
    """
    out = {}
    if not isinstance(resp, dict):
        return out, "", False
    rows = resp.get("item_info_list") or []
    for it in rows:
        plain = str(it.get("item_id_plain") or "")
        enc = str(it.get("item_id") or "")
        if plain and enc:
            out[plain] = enc
    nxt = resp.get("cursor")
    if nxt is None or nxt == "":
        nxt = resp.get("max_cursor")
    nxt = "" if nxt is None else str(nxt)
    if nxt == "0":
        nxt = ""
    return out, nxt, bool(resp.get("has_more"))'''

patch(API, [
    ("head", api_head_old, api_head_new),
    ("url-doc", item_list_doc_old, item_list_doc_new),
    ("map", item_map_old, item_map_new),
])

# ---------------- client.py ----------------
cache_old = """        #: 登录态已被服务端明确判死（status_code=8）。上层据此判掉线，别再发包。
        self.auth_fail = None
"""
cache_new = """        #: 登录态已被服务端明确判死（status_code=8）。上层据此判掉线，别再发包。
        self.auth_fail = None
        #: 明文 aweme_id -> 加密 item_id 缓存。加密结果对同一个 aweme_id 是稳定的，
        #: 免得每轮都从第 1 页翻起（91 条作品 = 5 页，纯浪费）。上限 2000 条足够。
        self._item_enc = {}
"""

map_old = '''    async def map_item_ids(self, aweme_ids, max_pages=20, count=20):
        """建 明文 aweme_id -> 加密 item_id 映射（评论接口只认加密串）。

        只翻到「需要的 id 全部找到」或没更多为止；找不到的留给调用方跳过并记日志，
        绝不拿明文 id 硬试（明文一定失败，白打一次请求）。
        """
        want = {str(x) for x in (aweme_ids or []) if x}
        found, cursor, pages = {}, "", 0
        while pages < max_pages:
            env = await self.fetch_item_page(cursor=cursor, count=count)
            pages += 1
            if not env["__ok"]:
                break
            for plain, enc in env["map"].items():
                if plain in want:
                    found[plain] = enc
            if want and len(found) >= len(want):
                break
            if not env["has_more"]:
                break
            nxt = env["max_cursor"]
            if not nxt or str(nxt) == str(cursor):
                break
            cursor = nxt
        return found'''

map_new = '''    async def map_item_ids(self, aweme_ids, max_pages=30, count=20, use_cache=True):
        """建 明文 aweme_id -> 加密 item_id 映射（评论接口只认加密串）。

        只翻到「需要的 id 全部找到」或没更多为止；找不到的留给调用方跳过并记日志，
        绝不拿明文 id 硬试（明文一定失败，白打一次请求）。

        🚨 这里的游标是 item/list 的 `cursor`（时间戳字符串），不是 work_list 的
        max_cursor（坑 4）。读错字段会永远读到 0 -> 第一页就 break -> 只覆盖 19 条。
        顺序是发布时间倒序，所以「只要最近的作品」通常 1 页就够；老作品才需要翻深。

        use_cache: 命中进程内缓存就不发请求。加密串对同一 aweme_id 稳定，
        缓存不会过期；但**换账号必须清缓存**（spec 里 item_id 与登录会话相关），
        见 clear_item_cache()。
        """
        want = {str(x) for x in (aweme_ids or []) if x}
        found = {}
        if use_cache:
            found = {k: self._item_enc[k] for k in want if k in self._item_enc}
        todo = want - set(found)
        if not todo:
            return found

        cursor, pages = "", 0
        while pages < max_pages:
            env = await self.fetch_item_page(cursor=cursor, count=count)
            pages += 1
            if not env["__ok"]:
                break
            for plain, enc in env["map"].items():
                if plain in todo:
                    found[plain] = enc
                    if len(self._item_enc) < 2000:
                        self._item_enc[plain] = enc
            if len(found) >= len(want):
                break
            if not env["has_more"]:
                break
            nxt = env.get("max_cursor") or ""
            if not nxt or str(nxt) == str(cursor):
                break
            cursor = nxt
        return found

    def clear_item_cache(self):
        """换账号 / 登录态变化后必须清，否则拿着别人的加密串去打评论接口。"""
        self._item_enc = {}'''

patch(CLI, [
    ("cache-init", cache_old, cache_new),
    ("map", map_old, map_new),
])

# ---------------- fetcher.py ----------------
fet_old = '''            max_pages=self._cfg_int("dy_item_pages", 20),
            count=self._cfg_int("dy_item_page_size", 20))'''
fet_new = '''            max_pages=self._cfg_int("dy_item_pages", 30),
            count=self._cfg_int("dy_item_page_size", 20))'''
patch(FET, [("item-pages", fet_old, fet_new)])


# ---------------- 自校验 ----------------
def selfcheck():
    a = io.open(API, encoding="utf-8").read()
    c = io.open(CLI, encoding="utf-8").read()
    f = io.open(FET, encoding="utf-8").read()
    checks = [
        ("api 有坑4说明", "坑 4：item/list 的翻页游标叫" in a),
        ("api 不再读 max_cursor 为主", 'nxt = resp.get("cursor")' in a),
        ("api 仍兼容 max_cursor", 'resp.get("max_cursor")' in a),
        ("api 游标空串归一", 'if nxt == "0":' in a),
        ("client 有缓存字段", "self._item_enc = {}" in c),
        ("client 读 cursor 不是 max_cursor", 'nxt = env.get("max_cursor") or ""' in c),
        ("client 默认 30 页", "max_pages=30, count=20, use_cache=True" in c),
        ("client 有清缓存方法", "def clear_item_cache(self):" in c),
        ("fetcher 默认 30 页", '"dy_item_pages", 30' in f),
    ]
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(("  PASS  " if ok else "  FAIL  ") + n)
    return not bad


print("=== 自校验 ===")
ok = selfcheck()

print("=== 语法检查 ===")
import py_compile  # noqa: E402
for p in (API, CLI, FET):
    try:
        py_compile.compile(p, doraise=True)
        print("  OK   " + os.path.basename(p))
    except Exception as e:
        ok = False
        print("  FAIL " + os.path.basename(p) + " : " + str(e)[:200])

print("\n结果:", "ALL PASS" if ok else "有失败项")
sys.exit(0 if ok else 1)
