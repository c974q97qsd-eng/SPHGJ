# -*- coding: utf-8 -*-
"""回归:账号名抓取不得退化成 finder_id 前缀占位。

锁定 2026-09-26 线上事故(某账号的名字被写成 finder_id 前缀占位):
  点击「选择视频号」后微信跳转到 /platform,旧 page 被关闭,
  而 self.page 仍指着它 -> auth_data 全部抛 Target page...has been closed,
  auth_finder_list 兜底反查也在同一个死 page 上失败 -> 名字退化。

跑法(需便携运行时):
  ./配置环境安装/Python314/python.exe tools/test_login_name.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.login_capture import LoginSession

PASS = FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


# ---------------- 假浏览器 ----------------
class ClosedError(Exception):
    pass


class FakePage:
    """按 eval 脚本内容分流返回;closed=True 时任何 evaluate 都抛。

    close_after_store=True:读 localStorage(含 finder_username)那一次成功后
    立刻把自己标记为已关闭 —— 精确模拟线上「点完选择账号跳转,旧 page 被关」。
    """

    def __init__(self, url="", closed=False, auth_data=None, finder_list=None,
                 store=None, close_after_store=False):
        self.url = url
        self._closed = closed
        self.auth_data = auth_data
        self.finder_list = finder_list
        self.store = store if store is not None else {}
        self.close_after_store = close_after_store
        self.handlers = {}
        self.eval_log = []
        self.goto_log = []

    def is_closed(self):
        return self._closed

    def on(self, ev, fn):
        self.handlers[ev] = fn

    async def wait_for_timeout(self, ms):
        if self._closed:
            raise ClosedError("Target page, context or browser has been closed")
        return None

    async def goto(self, url, **kw):
        if self._closed:
            raise ClosedError("Target page, context or browser has been closed")
        self.goto_log.append(url)
        return None

    async def evaluate(self, js, arg=None):
        if self._closed:
            raise ClosedError("Target page, context or browser has been closed")
        self.eval_log.append(js[:60])
        if "localStorage" in js and "finder_username" in js:
            r = {"aid": self.store.get("aid", ""),
                 "finder_id": self.store.get("finder_id", "")}
            if self.close_after_store:
                self._closed = True     # 读完就关(模拟跳转关掉旧 page)
            return r
        if "__ml::aid" in js and "const a =" in js:
            return self.store.get("aid", "")
        if "auth_data" in js:
            return self.auth_data
        if "auth_finder_list" in js:
            return self.finder_list
        return ""


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        p = FakePage(url="about:blank")
        self.pages.append(p)
        return p


class DeadContext(FakeContext):
    """context 已关:new_page 也开不出来。"""

    async def new_page(self):
        self.new_page_calls += 1
        raise RuntimeError("Target page, context or browser has been closed")


def mk_session(page, context, picked=""):
    s = LoginSession(playwright=None, config={}, emit=None)
    s.page = page
    s.context = context
    s._picked_name = picked
    return s


FINDER_ID = "v2_0000000000000000000000000000000000000000000000000000000000000000000000000000@finder"
TRUE_NAME = "U.S.POLOASSN.鞋类店"

# auth_finder_list:昵称有、finderUsername 为空(线上就是这样,按 id 反查必然落空)
FINDER_LIST_TXT = ('{"errCode":0,"data":{"finderList":['
                   '{"nickname":"抖星1号US4号","finderUsername":""},'
                   '{"nickname":"U.S.POLOASSN.尚品仓","finderUsername":""},'
                   '{"nickname":"U.S.POLOASSN.鞋类店","finderUsername":""}]}}')
# auth_data:接口没返回 finderUser(取不到名字)
AUTH_DATA_EMPTY = '{"errCode":0,"data":{}}'


async def test_ensure_page_alive():
    print("\n[A] _ensure_page_alive 各分支")
    dead = FakePage(url="https://channels.weixin.qq.com/login.html", closed=True)
    alive = FakePage(url="https://channels.weixin.qq.com/platform")
    s = mk_session(dead, FakeContext([dead, alive]))
    check(await s._ensure_page_alive() is True, "page 关闭时返回 True")
    check(s.page is alive, "已重新附着到活着的 page")

    s2 = mk_session(alive, FakeContext([alive]))
    check(await s2._ensure_page_alive() is True, "page 活着时直接返回 True")
    check(s2.page is alive, "page 活着时不切换")

    # 全死 -> new_page,且必须导航到同源页(about:blank 会让 fetch 被 CORS 拦)
    d1 = FakePage(url="a", closed=True)
    ctx = FakeContext([d1])
    s3 = mk_session(d1, ctx)
    check(await s3._ensure_page_alive() is True, "无活 page 时新开一个")
    check(ctx.new_page_calls == 1, "确实调用了 new_page")
    check(len(s3.page.goto_log) == 1 and "channels.weixin.qq.com" in s3.page.goto_log[0],
          "新开的 page 已导航到同源平台页(保证 fetch 不被 CORS 拦)")

    # 新开也失败 -> False
    d2 = FakePage(url="b", closed=True)
    s4 = mk_session(d2, DeadContext([d2]))
    check(await s4._ensure_page_alive() is False, "new_page 也失败时返回 False")

    s5 = mk_session(FakePage(closed=True), None)
    check(await s5._ensure_page_alive() is False, "context 为空时返回 False")


async def test_finder_list_cache():
    print("\n[B] auth_finder_list 缓存回退")
    # new_page 也开不出来 -> 只能吃缓存
    dead = FakePage(url="x", closed=True)
    s = mk_session(dead, DeadContext([dead]))
    s._finder_list_cache = [{"name": TRUE_NAME, "role": "", "finder_id": ""}]
    got = await s._fetch_finder_list()
    check(len(got) == 1 and got[0]["name"] == TRUE_NAME,
          "page 全不可用时复用缓存")

    dead2 = FakePage(url="x", closed=True)
    s2 = mk_session(dead2, DeadContext([dead2]))
    check(await s2._fetch_finder_list() == [], "page 不可用且无缓存时返回 []")

    live = FakePage(url="y", finder_list=FINDER_LIST_TXT)
    s3 = mk_session(live, FakeContext([live]))
    items = await s3._fetch_finder_list()
    check(len(items) == 3, "正常路径返回 3 个账号")
    check(len(s3._finder_list_cache) == 3, "结果已写入缓存")


async def test_hit_identity_guard():
    print("\n[C] auth_data 遇 page 关闭")
    dead = FakePage(url="x", closed=True)
    s = mk_session(dead, DeadContext([dead]))
    s.captured["aid"] = "aid-1"
    ident = await s._fetch_finder_identity()
    check(ident["nickname"] == "" and ident["finder_id"] == "",
          "page 全死时安全返回空(不抛异常)")


async def test_incident_replay():
    """核心:完整复现线上事故,断言名字不再退化。"""
    print("\n[D] 线上事故复现(2026-09-26 09:06)")
    dead = FakePage(url="https://channels.weixin.qq.com/login.html",
                    store={"aid": "aid-1", "finder_id": FINDER_ID},
                    close_after_store=True)      # localStorage 读完即关(模拟跳转)
    live = FakePage(url="https://channels.weixin.qq.com/platform",
                    auth_data=AUTH_DATA_EMPTY, finder_list=FINDER_LIST_TXT)
    s = mk_session(dead, FakeContext([dead, live]), picked=TRUE_NAME)
    await s._do_capture()
    check(s.captured["name"] == TRUE_NAME,
          f"name 兜底为真实名 {s.captured['name']!r}")
    check(not s.captured["name"].startswith("v2_"),
          "name 不再是 finder_id 前缀占位")
    check(s.captured["finder_id"] == FINDER_ID, "finder_id 仍正确保留")

    # 反证:没有 _picked_name 时会退化成占位(说明这一级兜底是决定性的)
    dead2 = FakePage(url="https://channels.weixin.qq.com/login.html",
                     store={"aid": "aid-1", "finder_id": FINDER_ID},
                     close_after_store=True)
    live2 = FakePage(url="https://channels.weixin.qq.com/platform",
                     auth_data=AUTH_DATA_EMPTY, finder_list=FINDER_LIST_TXT)
    s2 = mk_session(dead2, FakeContext([dead2, live2]), picked="")
    await s2._do_capture()
    check(s2.captured["name"] == FINDER_ID[:12],
          f"无 _picked_name 时退化为占位({s2.captured['name']!r}) —— 证明兜底链生效")


async def test_happy_path_unchanged():
    print("\n[E] 正常路径不受影响")
    live = FakePage(url="https://channels.weixin.qq.com/platform",
                    store={"aid": "aid-1", "finder_id": FINDER_ID},
                    auth_data='{"errCode":0,"data":{"finderUser":'
                              '{"nickname":"U.S.POLOASSN.彪壹男鞋店",'
                              '"finderUsername":"' + FINDER_ID + '"},'
                              '"userAttr":{"nickname":"u.s.投放6号"}}}')
    s = mk_session(live, FakeContext([live]))
    await s._do_capture()
    check(s.captured["name"] == "U.S.POLOASSN.彪壹男鞋店", "auth_data 正常时名字正确")
    check(s.captured["wx_name"] == "u.s.投放6号", "微信名正确")
    check(s.captured["aid"] == "aid-1", "aid 正确")


async def main():
    await test_ensure_page_alive()
    await test_finder_list_cache()
    await test_hit_identity_guard()
    await test_incident_replay()
    await test_happy_path_unchanged()
    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
