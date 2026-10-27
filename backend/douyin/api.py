"""抖音创作者中心（creator.douyin.com）接口规格层 —— **纯函数，不碰网络、不碰浏览器**。

这个文件只回答三件事：
  1. 某个功能对应的 URL / body 长什么样（三块功能：评论管理、作品管理、发布）
  2. 一个响应到底算不算成功（抖音的错误语义很坑，见下）
  3. 原始 JSON 怎么归一化成项目内部统一结构

把这三件事从 client.py 里抽出来的原因：它们是**最容易踩错、又最容易测**的部分。
client.py 只负责「把请求送出去」，api.py 全部可以离线单测
（tools/test_rev56_dy_api.py）。

======================================================================
🚨 三个必须记住的坑（全部真机实测，不是文档抄的）
======================================================================

**坑 1：`status_code == 0` 不代表成功。**
路径写错时，网关返回的是：
    {"status_code": 0, "status_msg": "url doesn't match"}
status_code 仍然是 0。唯一可靠的「打到了真接口」信号是响应里有 `extra` 字段。
    真接口成功: {"extra": {"now": ...}, "status_code": 0, "status_msg": ""}
    路径不对:   {"status_code": 0, "status_msg": "url doesn't match"}
    参数不对:   {"status_code": 18, "status_msg": "No item permission"}
👉 判定只看 `"extra" in resp`，见 judge()。`status_code: 18` 是「路径对了但参数名错」。

**坑 2：参数名大小写不一致，写错会静默失败。**
    回复评论 body: {"comment_Id": ...}   ← 大写 I
    删除评论 body: {"comment_id": ...}   ← 小写
    删除作品 body: {"item_id": ...}      ← 不是 aweme_id（用 aweme_id 会报 status_code 18）
这里全部集中成 reply_body() / delete_comment_body() / work_delete_body()，调用方不要自己拼。

**坑 3：同一个站里两种编码混用。**
    评论写接口（reply / delete）      -> application/json
    作品写接口（item/top、aweme/delete）-> application/x-www-form-urlencoded
    发布 create_v2                    -> application/json
见 *_FORM 常量与 form=True 标记。

另外两条实测结论：
  · 作品列表的 `max_cursor` 是**毫秒时间戳**，不是 offset；
  · 评论列表的 `item_id` 必须是**加密串**（@jPdq…==），明文 aweme_id 无效，
    加密串要从 /aweme/v1/creator/item/list 的 item_id 字段拿。
"""

import json
from urllib.parse import urlencode

ORIGIN = "https://creator.douyin.com"
AID = 2906  # 创作者中心固定值；发布提交用 read_aid=2906，两者一致

# 页面入口（page_url 决定 Referer 语义，也保证 fetch 同源）
PAGE_MANAGE = ORIGIN + "/creator-micro/content/manage"
PAGE_COMMENT = ORIGIN + "/creator-micro/interactive/comment"
PAGE_UPLOAD = ORIGIN + "/creator-micro/content/post/video"

# ---------------- 路径常量 ----------------
PATH_WORK_LIST = "/janus/douyin/creator/pc/work_list"
PATH_ITEM_LIST = "/aweme/v1/creator/item/list"
PATH_COMMENT_LIST = "/aweme/v1/creator/comment/list"
PATH_REPLY_LIST = "/aweme/v1/creator/comment/reply/list"
PATH_COMMENT_REPLY = "/aweme/v1/creator/comment/reply/"
PATH_COMMENT_DELETE = "/aweme/v1/creator/comment/delete/"
PATH_WORK_TOP = "/web/api/media/item/top/"
PATH_WORK_DELETE = "/web/api/media/aweme/delete/"

# 这几个是**表单编码**（坑 3）。别处一律 JSON。
FORM_PATHS = (PATH_WORK_TOP, PATH_WORK_DELETE)

# 工作列表固定带的一批客户端指纹参数。实测原样照抄即可，
# 缺了多数接口仍能用，但保持一致能少一次排查。
_CLIENT_QS = (
    ("cookie_enabled", "true"),
    ("screen_width", "3840"),
    ("screen_height", "2160"),
    ("browser_language", "zh-CN"),
    ("browser_platform", "Win32"),
    ("browser_name", "Mozilla"),
    ("browser_version", "5.0"),
    ("os_name", "Windows"),
    ("os_version", "10"),
    ("platform", "PC"),
    ("channel", "channel_pc_web"),
    ("aid", str(AID)),
)

# ---------------- 错误语义 ----------------
ERR_URL_MISMATCH = "url_mismatch"      # 路径不匹配（网关兜底返回，status_code 仍是 0）
ERR_BAD_PARAMS = "bad_params"          # status_code=18，路径对但参数名/格式错
ERR_NOT_LOGGED_IN = "not_logged_in"    # 未登录 / 登录态失效
ERR_EVAL = "eval_error"                # 页面求值本身失败（page 没了 / 超时）
ERR_HTTP = "http_error"                # fetch 抛错或非 2xx
ERR_PARSE = "parse_error"              # 响应不是 JSON

# 实测：status_code 8 = 未登录（抖音系通用）。命中即让上层判掉线，
# 但**只认这一个确定值**，其它奇怪码一律只当普通失败 —— 误判会把好账号踢出轮询。
_AUTH_CODES = (8,)


def _qs(pairs):
    return urlencode(list(pairs))


def common_qs(extra=None):
    """客户端指纹参数 + aid。extra 会追加在后面。"""
    pairs = list(_CLIENT_QS)
    if extra:
        pairs.extend(extra)
    return _qs(pairs)


# ==================== 1. 评论管理 ====================

def comment_list_url(item_id, cursor=0, count=20, sort="TIME"):
    """评论列表。

    item_id 必须是**加密串**（坑：明文 aweme_id 无效）。
    cursor 从 0 递增（不是时间戳，和作品列表不同）。
    sort: TIME / HOT
    """
    if not item_id:
        raise ValueError("comment_list_url: item_id 不能为空（需要加密 item_id）")
    return "%s?%s" % (PATH_COMMENT_LIST, _qs((
        ("cursor", str(int(cursor))),
        ("count", str(int(count))),
        # 不要在这里手工 quote：下面的 urlencode 会把 % 再编成 %25（双重编码），
        # 请求直接失败。urlencode 内部已做 %40 / %3D 全量编码。
        ("item_id", str(item_id)),
        ("sort", sort or "TIME"),
        ("aid", str(AID)),
    )))


def reply_list_url(comment_id, item_id, cursor=0, count=20):
    """一级评论下的子回复列表。结构与评论列表一致，子回复 level=2。"""
    if not comment_id or not item_id:
        raise ValueError("reply_list_url: comment_id / item_id 都不能为空")
    return "%s?%s" % (PATH_REPLY_LIST, _qs((
        ("comment_id", str(comment_id)),   # 编码交给 urlencode，理由同 comment_list_url
        ("item_id", str(item_id)),
        ("cursor", str(int(cursor))),
        ("count", str(int(count))),
        ("aid", str(AID)),
    )))


def comment_reply_url():
    return "%s?aid=%s" % (PATH_COMMENT_REPLY, AID)


def comment_reply_body(comment_id, item_id, text):
    """回复评论 body。

    🚨 键名是 `comment_Id`（大写 I）。历史上这里写小写不会报错，只会静默失败，
       看起来「回复成功了」但作品下面根本没有这条回复。
    """
    if not comment_id or not item_id:
        raise ValueError("comment_reply_body: comment_id / item_id 都不能为空")
    if not str(text or "").strip():
        raise ValueError("comment_reply_body: 回复内容不能为空")
    return {"comment_Id": str(comment_id), "item_id": str(item_id), "text": str(text)}


def comment_delete_url():
    return "%s?aid=%s" % (PATH_COMMENT_DELETE, AID)


def comment_delete_body(comment_id, item_id):
    """删除评论 body。🚨 这里键名是**小写** `comment_id`，和 reply 正好相反。"""
    if not comment_id or not item_id:
        raise ValueError("comment_delete_body: comment_id / item_id 都不能为空")
    return {"comment_id": str(comment_id), "item_id": str(item_id)}


# ==================== 2. 作品管理 ====================

def work_list_url(max_cursor=0, count=12, status=0):
    """作品列表（主用）。

    🚨 max_cursor 是**毫秒时间戳**：首页传 0，下一页取响应里的 max_cursor 原样回传。
       count 实测 12 可用（官方前端口径），不要自己放大。
       status：0 = 全部。
    """
    return "%s?%s" % (PATH_WORK_LIST, _qs((
        ("scene", "star_atlas"),
        ("device_platform", "android"),
        ("status", str(int(status or 0))),
        ("count", str(int(count or 12))),
        ("max_cursor", str(int(max_cursor or 0))),
    )) + "&" + common_qs())


def item_list_url(cursor="", count=20):
    """作品列表（带加密 item_id）。

    返回 item_id（加密，评论接口要用）+ item_id_plain（数字 aweme_id）。
    ⚠️ 排序与 work_list 不同，新作品不一定在第一页 —— 所以评论抓取不能只靠它，
       要用 work_list 的 aweme_id 去这里建映射（见 client.map_item_ids）。
    """
    return "%s?%s" % (PATH_ITEM_LIST, _qs((
        ("cursor", str(cursor or "")),
        ("aid", str(AID)),
        ("count", str(int(count or 20))),
    )))


def work_top_url():
    return "%s?%s" % (PATH_WORK_TOP, common_qs())


def work_top_body(aweme_id, top=True):
    """置顶 / 取消置顶。表单编码（坑 3）；参数是**明文** aweme_id，need_top=1|0。"""
    if not aweme_id:
        raise ValueError("work_top_body: aweme_id 不能为空")
    return {"aweme_id": str(aweme_id), "need_top": 1 if top else 0}


def work_delete_url():
    return "%s?%s" % (PATH_WORK_DELETE, common_qs())


def work_delete_body(aweme_id):
    """删除作品。表单编码；🚨 参数名是 `item_id`（值是明文 aweme_id），
    用 `aweme_id` 会返回 status_code=18 "No item permission"，极易误判成没权限。"""
    if not aweme_id:
        raise ValueError("work_delete_body: aweme_id 不能为空")
    return {"item_id": str(aweme_id)}


# ==================== 3. 响应判定 ====================

def is_hit(resp):
    """响应是不是真的打到了目标接口（坑 1 的唯一可靠信号）。"""
    return isinstance(resp, dict) and "extra" in resp


def status_code_of(resp):
    if not isinstance(resp, dict):
        return None
    v = resp.get("status_code")
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.lstrip("-").isdigit():
        return int(v)
    return None


def judge(resp):
    """返回 (ok, reason)。ok=True 才代表接口真的成功。

    判定顺序是刻意的：先看有没有 `extra`（坑 1），再看 status_code。
    """
    if not isinstance(resp, dict):
        return False, ERR_PARSE
    if not is_hit(resp):
        msg = str(resp.get("status_msg") or "")
        if "url doesn't match" in msg or "url not match" in msg.lower():
            return False, ERR_URL_MISMATCH
        return False, ERR_URL_MISMATCH if not msg else ERR_HTTP
    code = status_code_of(resp)
    if code in (0, None):
        return True, ""
    if code == 18:
        return False, ERR_BAD_PARAMS
    if code in _AUTH_CODES:
        return False, ERR_NOT_LOGGED_IN
    return False, "status_%s" % code


def is_auth_error(reason):
    return reason == ERR_NOT_LOGGED_IN


def explain(reason):
    """给日志/前端看的人话。"""
    return {
        ERR_URL_MISMATCH: "接口路径不匹配（多半是接口改版）",
        ERR_BAD_PARAMS: "参数名或编码不对（status_code=18）",
        ERR_NOT_LOGGED_IN: "登录态失效，需要重新扫码",
        ERR_EVAL: "页面执行失败（浏览器已关闭或超时）",
        ERR_HTTP: "请求失败（网络/网关）",
        ERR_PARSE: "响应不是 JSON",
    }.get(reason, reason or "未知错误")


# ==================== 4. 归一化 ====================

def _int(v, default=0):
    try:
        if isinstance(v, bool):
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _cover_url(raw):
    """封面直链。Cover.url_list 是带时效的 CDN 地址，只做展示用不落库为主键。"""
    cov = raw.get("Cover") or raw.get("cover") or {}
    if isinstance(cov, dict):
        urls = cov.get("url_list") or []
        if urls:
            return str(urls[0])
        uri = cov.get("uri") or ""
        if uri:
            return "https://p3-sign.douyinpic.com/" + str(uri)
    return ""


#: status_value 实测语义：102 公开正常；140/141 非公开（141 = 仅自己可见）
STATUS_PUBLIC = 102


def normalize_work(raw):
    """work_list 的一条 -> 项目内部作品结构。

    is_public 用 status_value == 102 判定（实测对照，不是猜的）。
    """
    raw = raw if isinstance(raw, dict) else {}
    author = raw.get("author") or {}
    stat = raw.get("statistics") or {}
    return {
        "aweme_id": str(raw.get("aweme_id") or ""),
        "desc": raw.get("desc") or "",
        "create_time": _int(raw.get("create_time")),
        "status_value": _int(raw.get("status_value"), -1),
        "is_public": _int(raw.get("status_value"), -1) == STATUS_PUBLIC,
        "duration": _int(raw.get("duration")),
        "cover_url": _cover_url(raw),
        "allow_comment": bool((raw.get("status") or {}).get("allow_comment", True)),
        "read_count": _int(stat.get("play_count")),
        "like_count": _int(stat.get("digg_count")),
        "comment_count": _int(stat.get("comment_count")),
        "share_count": _int(stat.get("share_count")),
        "author": {
            "uid": str(author.get("uid") or ""),
            "sec_uid": str(author.get("sec_uid") or ""),
            "unique_id": str(author.get("unique_id") or ""),
            "nickname": author.get("nickname") or "",
        },
        "raw": raw,
    }


def normalize_comment(raw):
    """评论列表的一条 -> 项目内部评论结构（对齐 storage.upsert_comment 的入参口径）。

    ⚠️ 抖音的 create_time / digg_count 是**字符串**，这里统一转 int，
       否则下游 int() 到处散落、最容易被漏掉一处。
    """
    raw = raw if isinstance(raw, dict) else {}
    user = raw.get("user_info") or raw.get("user") or {}
    return {
        "comment_id": str(raw.get("comment_id") or ""),
        "text": raw.get("text") or "",
        "create_time": _int(raw.get("create_time")),
        "like_count": _int(raw.get("digg_count")),
        "reply_count": _int(raw.get("reply_count")),
        "status": _int(raw.get("status")),
        "level": _int(raw.get("level"), 1),
        "is_author": bool(raw.get("is_author")),
        "nickname": user.get("screen_name") or user.get("nickname") or "",
        "author_id": str(user.get("user_id") or user.get("sec_uid") or ""),
        "head_url": user.get("avatar_url") or "",
        "raw": raw,
    }


def comments_of(resp):
    """从评论列表响应里取评论数组 + 分页信息。"""
    if not isinstance(resp, dict):
        return [], 0, False, 0
    items = resp.get("comment_info_list") or resp.get("comments") or []
    nxt = _int(resp.get("cursor"), 0)
    more = bool(resp.get("has_more"))
    return ([normalize_comment(x) for x in items], nxt, more, _int(resp.get("total_count")))


def works_of(resp):
    """从作品列表响应里取作品数组 + 分页信息。"""
    if not isinstance(resp, dict):
        return [], 0, False, 0
    items = resp.get("aweme_list") or []
    return ([normalize_work(x) for x in items],
            _int(resp.get("max_cursor")),
            bool(resp.get("has_more")),
            _int(resp.get("total")))


def item_map_of(resp):
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
    return out, _int(resp.get("max_cursor")), bool(resp.get("has_more"))


def dumps_form(body):
    """表单编码（坑 3）。值统一转字符串，None 视为空串。"""
    return urlencode([(k, "" if v is None else str(v)) for k, v in (body or {}).items()])


def dumps_json(body):
    return json.dumps(body or {}, ensure_ascii=False, separators=(",", ":"))
