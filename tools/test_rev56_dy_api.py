# -*- coding: utf-8 -*-
"""rev56 抖音适配层离线单测 —— 不联网、不开浏览器、不需要 playwright。

覆盖三块最容易错的东西：
  1. 平台抽象（老配置没有 platform 必须仍按 wx，归属键推导规则）
  2. 接口规格（URL 参数、编码选择、参数名大小写这三个坑）
  3. 响应判定（status_code=0 也可能是失败）与客户端信封语义

跑法: python tools/test_rev56_dy_api.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import platforms
from backend.douyin import api
from backend.douyin.client import DouyinClient

PASS = 0
FAIL = []


def ck(cond, name, extra=""):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL.append(name)
        print("  FAIL  %s  %s" % (name, extra))


# ==================== 1. 平台抽象 ====================

def t_platform():
    print("\n[1] 平台抽象")
    ck(platforms.normalize_platform(None) == "wx", "platform 缺省 = wx（老配置向后兼容）")
    ck(platforms.normalize_platform("") == "wx", "空串 = wx")
    ck(platforms.normalize_platform("抖音") == "dy", "中文别名 抖音 -> dy")
    ck(platforms.normalize_platform("douyin") == "dy", "douyin -> dy")
    ck(platforms.normalize_platform("creator") == "dy", "creator -> dy")
    ck(platforms.normalize_platform("channels") == "wx", "channels -> wx")
    ck(platforms.normalize_platform("垃圾值") == "wx", "认不出来按 wx")

    legacy = {"id": "uspoloassn1", "_log_finder_id": "v2_0600"}
    ck(platforms.account_platform(legacy) == "wx", "老账号（无 platform 字段）= wx")
    dy = {"id": "dyA", "platform": "dy"}
    ck(platforms.is_douyin(dy) and not platforms.is_douyin(legacy), "is_douyin 判定")

    ck(platforms.owner_key(legacy) == "v2_0600", "wx 归属键取 _log_finder_id")
    ck(platforms.owner_key({"id": "a", "locked_finder_id": "v2_x"}) == "v2_x",
       "wx 无 _log 时取 locked_finder_id")
    ck(platforms.owner_key({"id": "a"}) == "acct:a", "wx 都取不到 -> acct:<id>")
    ck(platforms.owner_key({"id": "b", "platform": "dy", "_dy_sec_uid": "SEC1",
                            "_dy_uid": "123"}) == "SEC1",
       "dy 归属键优先 sec_uid")
    ck(platforms.owner_key({"id": "b", "platform": "dy", "_dy_uid": "123"}) == "123",
       "dy 无 sec_uid 时退 uid")
    ck(platforms.owner_key({"id": "b", "platform": "dy"}) == "acct:b",
       "dy 都没有 -> acct:<id> 兜底")
    ck(platforms.owner_is_fallback("acct:x") and not platforms.owner_is_fallback("SEC1"),
       "owner_is_fallback")
    ck(platforms.platform_name("dy") == "抖音" and platforms.platform_name(None) == "视频号",
       "platform_name")


# ==================== 2. 接口规格 ====================

def t_urls():
    print("\n[2] URL / body 规格")
    u = api.work_list_url(max_cursor=1787918865000, count=12)
    ck(u.startswith(api.PATH_WORK_LIST + "?"), "work_list 路径")
    ck("max_cursor=1787918865000" in u, "max_cursor 是毫秒时间戳原样透传")
    ck("count=12" in u and "status=0" in u and "scene=star_atlas" in u, "work_list 固定参数")
    ck("aid=2906" in u, "aid=2906")
    ck("max_cursor=0" in api.work_list_url(), "默认首页 max_cursor=0")

    item = "@jPdq67RETkrwscVd7LLuo894qi6SZi3xLaokPqJuiq9BsgejRo0mWYw1IBd39et4f3Sv2GCRzvGKXCCkVj8NMg=="
    cu = api.comment_list_url(item, cursor=0, count=20)
    ck("item_id=" in cu and "@" not in cu.split("item_id=")[1].split("&")[0],
       "comment_list 的 item_id 做了 URL 编码")
    ck("%3D%3D" in cu, "= 也编码（safe='' 才编）")
    ck("cursor=0" in cu and "sort=TIME" in cu, "comment_list 默认 sort=TIME / cursor 数字")
    ck("sort=HOT" in api.comment_list_url(item, sort="HOT"), "sort 可指定 HOT")
    try:
        api.comment_list_url("")
        ck(False, "空 item_id 必须报错")
    except ValueError:
        ck(True, "空 item_id 必须报错（不能拿明文硬试）")

    ru = api.reply_list_url("C@1==", item, cursor=2)
    ck("comment_id=C%401%3D%3D" in ru and "cursor=2" in ru, "reply_list 两个 id 都编码")

    # 🚨 参数名大小写（坑 2）
    rb = api.comment_reply_body("C1", "I1", "你好")
    ck("comment_Id" in rb and "comment_id" not in rb, "回复评论键名 comment_Id（大写 I）")
    ck(rb["text"] == "你好" and rb["item_id"] == "I1", "回复评论 body 内容")
    db = api.comment_delete_body("C1", "I1")
    ck("comment_id" in db and "comment_Id" not in db, "删除评论键名 comment_id（小写）")
    wd = api.work_delete_body("7701470168337239296")
    ck("item_id" in wd and "aweme_id" not in wd,
       "删除作品参数名是 item_id（用 aweme_id 会 status_code=18）")
    wt = api.work_top_body("123", top=True)
    ck(wt == {"aweme_id": "123", "need_top": 1}, "置顶 need_top=1")
    ck(api.work_top_body("123", top=False)["need_top"] == 0, "取消置顶 need_top=0")

    for bad in (("", "I1", "x"), ("C1", "", "x"), ("C1", "I1", "  ")):
        try:
            api.comment_reply_body(*bad)
            ck(False, "回复空参数必须报错 %r" % (bad,))
        except ValueError:
            ck(True, "回复空参数必须报错 %r" % (bad,))

    # 坑 3：编码混用
    ck(api.PATH_WORK_TOP in api.FORM_PATHS, "置顶走表单编码")
    ck(api.PATH_WORK_DELETE in api.FORM_PATHS, "删作品走表单编码")
    ck(api.PATH_COMMENT_REPLY not in api.FORM_PATHS, "回复评论走 JSON")
    ck(api.PATH_COMMENT_DELETE not in api.FORM_PATHS, "删评论走 JSON")

    f = api.dumps_form({"item_id": "770", "need_top": 1})
    ck(f == "item_id=770&need_top=1", "表单编码正确: %s" % f)
    j = api.dumps_json({"a": "中文"})
    ck("中文" in j, "JSON 编码不转义中文")
    ck(api.dumps_form({"a": None}) == "a=", "表单 None -> 空串")


# ==================== 3. 响应判定（坑 1） ====================

def t_judge():
    print("\n[3] 响应判定")
    ck(api.judge({"extra": {"now": 1}, "status_code": 0, "status_msg": ""}) == (True, ""),
       "有 extra + status_code=0 -> 成功")
    ck(api.judge({"extra": {"logid": "x"}, "status_code": 0})[0], "有 extra 无 status_msg -> 成功")
    ok, why = api.judge({"status_code": 0, "status_msg": "url doesn't match"})
    ck(not ok and why == api.ERR_URL_MISMATCH,
       "status_code 仍是 0 但路径不对 -> 必须判失败（坑 1）")
    ok, why = api.judge({"extra": {}, "status_code": 18, "status_msg": "No item permission"})
    ck(not ok and why == api.ERR_BAD_PARAMS, "status_code=18 -> 参数名/编码错")
    ok, why = api.judge({"extra": {}, "status_code": 8})
    ck(not ok and why == api.ERR_NOT_LOGGED_IN, "status_code=8 -> 未登录")
    ck(api.is_auth_error(api.ERR_NOT_LOGGED_IN), "is_auth_error")
    ck(api.judge("not a dict") == (False, api.ERR_PARSE), "非 dict -> 解析失败")
    ok, why = api.judge({"extra": {}, "status_code": 2456})
    ck(not ok and why == "status_2456", "未知码只当普通失败（不误判掉线）")
    ck(api.explain(api.ERR_BAD_PARAMS) != api.ERR_BAD_PARAMS, "explain 有人话")


# ==================== 4. 归一化 ====================

def t_normalize():
    print("\n[4] 归一化")
    raw = {
        "aweme_id": 7680204627916726001, "desc": "文案", "status_value": 102,
        "create_time": 1787918865, "duration": 27034,
        "Cover": {"uri": "tos-cn-x", "url_list": ["https://cdn/1.jpg"]},
        "status": {"allow_comment": True},
        "statistics": {"play_count": 1200, "digg_count": 3, "comment_count": 4},
        "author": {"uid": "202729", "sec_uid": "SEC", "unique_id": "q487225",
                   "nickname": "测试号"},
    }
    w = api.normalize_work(raw)
    ck(w["aweme_id"] == "7680204627916726001", "aweme_id 转字符串（写操作用它）")
    ck(w["is_public"] is True, "status_value=102 -> 公开")
    ck(api.normalize_work({"status_value": 141})["is_public"] is False,
       "status_value=141 -> 非公开（仅自己可见）")
    ck(api.normalize_work({"status_value": 140})["is_public"] is False, "status_value=140 -> 非公开")
    ck(w["cover_url"] == "https://cdn/1.jpg", "封面取 url_list 第一项")
    ck(w["read_count"] == 1200 and w["comment_count"] == 4, "统计字段")
    ck(w["author"]["sec_uid"] == "SEC" and w["author"]["uid"] == "202729",
       "author 身份字段（归属键来源）")

    c = api.normalize_comment({
        "comment_id": "@jPdq==", "text": "内容", "create_time": "1788187732",
        "digg_count": "5", "reply_count": "1", "status": 1, "level": 2,
        "is_author": True,
        "user_info": {"screen_name": "桔梗", "user_id": "@jPdq", "avatar_url": "http://a"},
    })
    ck(c["create_time"] == 1788187732 and isinstance(c["create_time"], int),
       "评论时间字符串转 int")
    ck(c["like_count"] == 5 and c["reply_count"] == 1, "点赞/回复数转 int")
    ck(c["level"] == 2 and c["is_author"] is True, "level / is_author")
    ck(c["nickname"] == "桔梗" and c["author_id"] == "@jPdq" and c["head_url"] == "http://a",
       "用户字段映射")
    ck(api.normalize_comment({})["level"] == 1, "缺 level 时默认 1（一级评论）")

    rows, nxt, more, total = api.comments_of(
        {"comment_info_list": [{"comment_id": "a"}, {"comment_id": "b"}],
         "cursor": 2, "has_more": True, "total_count": 9})
    ck(len(rows) == 2 and nxt == 2 and more is True and total == 9, "comments_of")
    ck(api.comments_of(None)[0] == [], "comments_of(None) 安全")

    works, cur, more2, total2 = api.works_of(
        {"aweme_list": [{"aweme_id": "1"}], "max_cursor": 1787918865000,
         "has_more": True, "total": 91})
    ck(len(works) == 1 and cur == 1787918865000 and more2 and total2 == 91, "works_of")

    m, _, _ = api.item_map_of({"item_info_list": [
        {"item_id": "ENC1", "item_id_plain": "111"},
        {"item_id": "", "item_id_plain": "222"}]})
    ck(m == {"111": "ENC1"}, "item_map_of 只收成对的行")


# ==================== 5. 客户端信封语义 ====================

class FakeTransport:
    """按队列返回预设响应；记录每次调用的 (method, url, body, form)。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    async def send(self, method, url, body=None, form=False):
        self.calls.append({"method": method, "url": url, "body": body, "form": form})
        if not self.replies:
            return 200, json.dumps({"extra": {"now": 1}, "status_code": 0})
        r = self.replies.pop(0)
        if isinstance(r, tuple):
            return r
        return 200, json.dumps(r)


class FakeStorage:
    def __init__(self, n=0):
        self.n = n
        self.incr = 0

    def get_daily_write_count(self, acc, day):
        return self.n

    def incr_daily_write_count(self, acc, day):
        self.n += 1
        self.incr += 1


def _client(replies=None, storage=None, account=None):
    acc = account or {"id": "dyA", "platform": "dy", "_dy_sec_uid": "SEC"}
    t = FakeTransport(replies)
    c = DouyinClient(None, acc, config={"risk_control": {"dy_read_interval": [0, 0],
                                                        "dy_write_interval": [0, 0]}},
                     storage=storage, transport=t)
    return c, t


async def t_client():
    print("\n[5] 客户端信封与编码路由")

    # 5.1 成功信封
    c, t = _client([{"extra": {"now": 1}, "status_code": 0,
                     "aweme_list": [{"aweme_id": "1", "status_value": 102}],
                     "max_cursor": 123, "has_more": True, "total": 2}])
    env = await c.fetch_work_page(max_cursor=0, count=12)
    ck(env["__ok"] and len(env["works"]) == 1 and env["max_cursor"] == 123,
       "fetch_work_page 成功信封")
    ck(t.calls[0]["method"] == "GET" and not t.calls[0]["form"], "读接口 GET + 不表单编码")

    # 5.2 url doesn't match 必须判失败（否则会把空列表当成功，误判作品被删光）
    c, t = _client([{"status_code": 0, "status_msg": "url doesn't match"}])
    env = await c.fetch_work_page()
    ck(not env["__ok"] and env["__err"] == api.ERR_URL_MISMATCH,
       "路径不匹配 -> __ok=False（不会静默返回空列表）")
    ck(c._consec_fail == 1 and c._backoff_until > 0, "失败计入退避")
    c._consec_fail = 0
    c._backoff_until = 0

    # 5.3 未登录 -> auth_fail 置位
    c, t = _client([{"extra": {}, "status_code": 8}])
    env = await c.fetch_work_page()
    ck(not env["__ok"] and c.auth_fail is not None, "status_code=8 -> auth_fail 置位（供判掉线）")
    c._backoff_until = 0

    # 5.4 回复评论：JSON 编码 + 正确键名
    st = FakeStorage(0)
    c, t = _client([{"extra": {"now": 1}, "status_code": 0}], storage=st)
    env = await c.reply_comment("C1", "I1", "测试回复")
    ck(env["__ok"], "reply_comment 成功")
    call = t.calls[0]
    ck(call["method"] == "POST" and call["form"] is False, "回复评论走 JSON")
    ck(call["body"].get("comment_Id") == "C1" and "comment_id" not in call["body"],
       "回复评论 body 键名 comment_Id")
    ck(st.incr == 1, "写成功后写计数 +1")

    # 5.5 置顶：表单编码
    c, t = _client([{"extra": {"now": 1}, "status_code": 0}])
    env = await c.top_work("770", top=True)
    ck(env["__ok"] and t.calls[0]["form"] is True, "置顶走表单编码")
    ck(t.calls[0]["body"] == {"aweme_id": "770", "need_top": 1}, "置顶 body")

    # 5.6 删作品：表单 + item_id
    c, t = _client([{"extra": {"logid": "x"}, "status_code": 0}])
    env = await c.delete_work("770")
    ck(env["__ok"] and t.calls[0]["form"] and t.calls[0]["body"] == {"item_id": "770"},
       "删作品：表单 + item_id")

    # 5.7 写上限：达限后不发请求
    c, t = _client([], storage=FakeStorage(100))
    env = await c.reply_comment("C1", "I1", "x")
    ck(not env["__ok"] and env["__err"] == "daily_write_limit", "达每日写上限 -> 拒绝")
    ck(len(t.calls) == 0, "达上限时一个请求都不发（不白撞）")
    ck(c.write_blocked() is True, "write_blocked()")

    # 5.8 参数校验失败不发请求
    c, t = _client([])
    env = await c.reply_comment("C1", "I1", "   ")
    ck(not env["__ok"] and env["__err"] == "bad_args" and len(t.calls) == 0,
       "空回复内容 -> bad_args，不发请求")

    # 5.9 page 为空
    c, t = _client([])
    c.set_page(None)
    c.transport = None
    c.transport = type("T", (), {"send": lambda *a, **k: _no_page()})()
    env = await c.fetch_work_page()
    ck(not env["__ok"] and env["__err"] == api.ERR_EVAL, "无 page -> eval_error（不抛异常）")
    c._backoff_until = 0

    # 5.10 fetch_all_works 完整性
    c, t = _client([
        {"extra": {}, "aweme_list": [{"aweme_id": "1"}], "max_cursor": 100, "has_more": True},
        {"extra": {}, "aweme_list": [{"aweme_id": "2"}], "max_cursor": 200, "has_more": False},
    ])
    works, complete = await c.fetch_all_works()
    ck(len(works) == 2 and complete, "fetch_all_works 翻两页")
    c, t = _client([
        {"extra": {}, "aweme_list": [{"aweme_id": "1"}], "max_cursor": 100, "has_more": True},
        {"status_code": 0, "status_msg": "url doesn't match"},
    ])
    works, complete = await c.fetch_all_works()
    ck(len(works) == 1 and complete is False,
       "中途失败 -> complete=False（不能把残缺当全量）")

    # 5.11 游标不前进必须停（防死循环）
    c, t = _client([{"extra": {}, "aweme_list": [{"aweme_id": "1"}],
                     "max_cursor": 100, "has_more": True}] * 3)
    works, complete = await c.fetch_all_works(max_pages=10)
    ck(len(t.calls) <= 2, "max_cursor 不前进 -> 立即停（不死循环），实际请求 %d 次" % len(t.calls))

    # 5.12 map_item_ids
    c, t = _client([
        {"extra": {}, "item_info_list": [{"item_id": "E1", "item_id_plain": "1"}],
         "max_cursor": 500, "has_more": True},
        {"extra": {}, "item_info_list": [{"item_id": "E2", "item_id_plain": "2"}],
         "max_cursor": 400, "has_more": False},
    ])
    m = await c.map_item_ids(["1", "2"])
    ck(m == {"1": "E1", "2": "E2"}, "map_item_ids 建明文->加密映射")
    ck(len(t.calls) == 2, "找到全部后不再多翻页")

    # 5.13 self_identity
    c, t = _client([{"extra": {}, "aweme_list": [{"aweme_id": "1",
                                                  "author": {"uid": "9", "sec_uid": "S"}}],
                     "has_more": False}])
    ident = await c.self_identity()
    ck(ident.get("uid") == "9" and ident.get("sec_uid") == "S", "self_identity 取 author")
    c, t = _client([{"extra": {}, "aweme_list": [], "has_more": False}])
    ck(await c.self_identity() == {}, "没有作品 -> {} （继续用 acct: 兜底，不瞎猜）")


async def _no_page():
    return -1, "ERR no_page"


def main():
    t_platform()
    t_urls()
    t_judge()
    t_normalize()
    asyncio.run(t_client())
    print("\n" + "=" * 60)
    print("合计 PASS %d / FAIL %d" % (PASS, len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
