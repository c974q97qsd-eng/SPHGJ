"""平台维度：wx = 视频号，dy = 抖音。

为什么要有这个文件
------------------
SPHGJ 原来只服务视频号，归属主键是 **finder_id（视频号 ID）**，8 张表全按它归属
（见 storage.py 顶部注释）。接入抖音时【没有拆主键】，而是把 `finder_id` 的语义
扩成「**平台内的账号本体 id**」：

    wx -> 视频号 finder_id      v2_060000231003b20faec8c4e08d1fc7d4ce00e
    dy -> 抖音 sec_uid         MS4wLjABAAAA...（取不到时降级 dy_uid 数字串）

另加一个 `platform` 列做域标记（默认 'wx'，历史数据零迁移）。这么做而不是拆成
(platform, owner_id) 复合主键，理由有两条：

  1. finder_id 在各自平台内本来就是唯一的，加 platform 进主键只会逼着 250 处
     SQL / 索引 / UNIQUE 约束全改一遍，收益为零、回归风险很大；
  2. 真正需要「区分平台」的地方只有三类：展示分组、跨平台统计、防止两平台
     同值撞车（实际不会撞）。第一种查列就够，后两种加 WHERE 也行。

所以本模块只做三件事：
  - 平台常量与归一化（老配置没有 platform 字段一律按 wx，保证向后兼容）
  - 账号 -> 平台 判定
  - 账号 -> 归属主键(owner key) 推导，wx / dy 各一套规则

⚠️ 抖音侧的 sec_uid 只能在**登录后**从作品列表的 author.sec_uid 拿到；
   未登录时一律返回 "acct:<id>" 兜底（与 storage.cover_key 的兜底约定一致），
   等抓到作品后由调用方回填 `_dy_sec_uid`，不要在这里猜。
"""

PLATFORM_WX = "wx"
PLATFORM_DY = "dy"
PLATFORM_NAMES = {PLATFORM_WX: "视频号", PLATFORM_DY: "抖音"}
ALL_PLATFORMS = (PLATFORM_WX, PLATFORM_DY)


def normalize_platform(value, default=PLATFORM_WX):
    """把配置/请求里的 platform 归一成常量。认不出来的按 default（默认 wx）。

    兼容常见写法：'wx' / 'weixin' / 'shipinhao' / 'channels' / '视频号'；
                 'dy' / 'douyin' / 'douyin_creator' / '抖音'。
    """
    v = str(value or "").strip().lower()
    if not v:
        return default
    if v in ("wx", "weixin", "wechat", "shipinhao", "channels", "视频号"):
        return PLATFORM_WX
    if v in ("dy", "douyin", "douyin_creator", "creator", "抖音"):
        return PLATFORM_DY
    return default


def account_platform(acc):
    """账号属于哪个平台。老配置没有 platform 字段 -> wx（向后兼容的唯一入口）。"""
    if not isinstance(acc, dict):
        return PLATFORM_WX
    return normalize_platform(acc.get("platform"))


def is_douyin(acc):
    return account_platform(acc) == PLATFORM_DY


def platform_name(value):
    return PLATFORM_NAMES.get(normalize_platform(value), "视频号")


def owner_fallback(account_id):
    """拿不到平台本体 id 时的兜底归属键。

    与 storage.cover_key 的 "acct:" 约定一致；目录名会把冒号换成下划线。
    宁可先标「未知归属」，也绝不丢数据（迁移函数之后会换成真值）。
    """
    return "acct:" + (str(account_id or "").strip() or "unknown")


def owner_key(acc, account_id=None):
    """账号 -> 归属主键（存进各表的 finder_id 列）。

    wx: _log_finder_id -> locked_finder_id -> acct:<id>
    dy: _dy_sec_uid    -> _dy_uid        -> acct:<id>

    ⚠️ 抖音优先 sec_uid 而不是数字 uid：uid 是账号的内部数字 id，sec_uid 才是
       对外稳定标识（分享链接、主页 URL 都用它），换设备/换登录方式也不会变。
    """
    acc = acc if isinstance(acc, dict) else {}
    aid = account_id or acc.get("id") or ""
    if account_platform(acc) == PLATFORM_DY:
        for k in ("_dy_sec_uid", "_dy_uid"):
            v = str(acc.get(k) or "").strip()
            if v:
                return v
        return owner_fallback(aid)
    for k in ("_log_finder_id", "locked_finder_id"):
        v = str(acc.get(k) or "").strip()
        if v:
            return v
    return owner_fallback(aid)


def owner_is_fallback(key):
    """归属键是不是兜底值（= 还没拿到平台本体 id）。"""
    return str(key or "").startswith("acct:")


def account_brief(acc):
    """给日志/前端用的简短标识：平台 + 名字。"""
    acc = acc if isinstance(acc, dict) else {}
    return "[%s] %s" % (platform_name(account_platform(acc)),
                        acc.get("name") or acc.get("id") or "?")
