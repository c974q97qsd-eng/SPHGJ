"""rev52 达限停写验证:_maybe_auto_comment 是否真的挡住 try_comment。

背景:线上 run.log 里 5573 条 {'__err':'daily_write_limit'} WARNING(5572 自动评论失败
+ 1 置顶失败),全部产生于 daily_write_count 到达 100 的那几天。rev52 在
_maybe_auto_comment 入口加了 write_blocked() 判断,但线上这两天计数只有 2~5,
真实触发路径没被走到 —— 所以用单测把「达限必跳过」这条不变量钉死。

断言:
  [1] write_blocked()=True  -> try_comment 一次都不调(核心不变量)
  [2] write_blocked()=False -> try_comment 正常调
  [3] api 没有 write_blocked 方法(老版本/测试桩) -> 不崩,照常调
  [4] write_blocked() 自己抛异常 -> 不崩,保守照常调(不能因为计数出错就不发评论)
  [5] WxApiClient.write_blocked 与 _post 的判定口径一致(都走 _daily_count)
  [6] 计数持久化:storage 有就写库、没有就内存按日重置
"""
import asyncio
import sys
import types

sys.path.insert(0, r"D:\SPHGJ")

# 本地无 playwright(它只装在母本),stub 掉只为能 import account_manager 链路
for name in ("playwright", "playwright.async_api", "playwright._impl",
             "playwright._impl._errors"):
    m = types.ModuleType(name)
    m.Error = type("Error", (Exception,), {})
    m.TimeoutError = type("TimeoutError", (Exception,), {})
    m.async_api = sys.modules.get("playwright.async_api", m)
    sys.modules.setdefault(name, m)

from backend.comment_fetcher import CommentFetcher   # noqa: E402
from backend.api_client import WxApiClient           # noqa: E402

PASS = 0
FAIL = []


def chk(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL  {msg}")


class FakeCommenter:
    def __init__(self):
        self.calls = []

    async def try_comment(self, oid):
        self.calls.append(oid)
        return True


class FakeApi:
    def __init__(self, blocked=False, boom=False, has_method=True):
        self.blocked = blocked
        self.boom = boom
        if not has_method:
            # 模拟老版本 api_client:没有 write_blocked
            self.__class__ = type("OldApi", (), {})

    def write_blocked(self):
        if self.boom:
            raise RuntimeError("count table missing")
        return self.blocked


def make_fetcher(api):
    fc = CommentFetcher(api, storage=None, account_id="uspoloassn8",
                        auto_commenter=FakeCommenter())
    return fc


def run(fn):
    return asyncio.run(fn)


print("[1] write_blocked()=True -> 不调 try_comment")
api = FakeApi(blocked=True)
fc = make_fetcher(api)
run(fc._maybe_auto_comment("export/AAA"))
chk(fc.auto_commenter.calls == [], "达限时 try_comment 零调用(核心不变量)")

print("[2] write_blocked()=False -> 正常调")
api = FakeApi(blocked=False)
fc = make_fetcher(api)
run(fc._maybe_auto_comment("export/BBB"))
chk(fc.auto_commenter.calls == ["export/BBB"], "未达限时 try_comment 正常调用")

print("[3] api 无 write_blocked 方法 -> 不崩,照常调")
api = FakeApi(has_method=False)
fc = make_fetcher(api)
chk(not hasattr(api, "write_blocked"), "桩确实没有该方法")
run(fc._maybe_auto_comment("export/CCC"))
chk(fc.auto_commenter.calls == ["export/CCC"], "老 api 不崩且照常调用(向后兼容)")

print("[4] write_blocked() 抛异常 -> 不崩,保守照常调")
api = FakeApi(boom=True)
fc = make_fetcher(api)
run(fc._maybe_auto_comment("export/DDD"))
chk(fc.auto_commenter.calls == ["export/DDD"], "计数异常时保守放行(不漏发)")

print("[5] try_comment 内部异常不外泄")
class BoomCommenter:
    async def try_comment(self, oid):
        raise RuntimeError("network gone")

fc = CommentFetcher(None, storage=None, account_id="x", auto_commenter=BoomCommenter())
try:
    run(fc._maybe_auto_comment("export/EEE"))
    chk(True, "try_comment 抛错被吞掉,未打断抓取轮")
except Exception as e:
    chk(False, f"异常外泄: {e}")

print("[6] WxApiClient.write_blocked 与 _post 口径一致")
acc = {"id": "uspoloassn8", "_aid": "a", "_log_finder_id": "f"}


class FakeStorage:
    def __init__(self, n):
        self.n = n

    def get_daily_write_count(self, account_id, date_str):
        return self.n

    def incr_daily_write_count(self, account_id, date_str):
        self.n += 1
        return self.n


cli = WxApiClient(None, acc, {"risk_control": {"daily_write_limit": 100}}, FakeStorage(100))
chk(cli.write_blocked() is True, "计数=上限 -> write_blocked True(与 _post 的 >= 一致)")

cli2 = WxApiClient(None, acc, {"risk_control": {"daily_write_limit": 100}}, FakeStorage(99))
chk(cli2.write_blocked() is False, "计数=上限-1 -> write_blocked False")

# _post 真被挡住时不发请求(用 page=None + http 关闭,若没挡住会抛 AttributeError)
cli3 = WxApiClient(None, acc, {"risk_control": {"daily_write_limit": 100,
                                                "api_write_via": "browser"}},
                   FakeStorage(100))
r = run(cli3._post("create_comment", {"content": "x"}))
chk(isinstance(r, dict) and r.get("__err") == "daily_write_limit",
    "_post 达限直接返回 daily_write_limit,未尝试任何网络/浏览器请求")

print("[7] storage 缺失时内存计数按日重置")
cli4 = WxApiClient(None, acc, {"risk_control": {"daily_write_limit": 2}}, None)
cli4._daily_writes = 2
cli4._daily_writes_day = __import__("time").strftime("%Y-%m-%d")
chk(cli4.write_blocked() is True, "内存计数达限 -> True")
cli4._daily_writes_day = "1970-01-01"
chk(cli4.write_blocked() is False, "跨日自动归零 -> False(不会永久停写)")

print()
print(f"{PASS} PASS / {len(FAIL)} FAIL")
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1)
