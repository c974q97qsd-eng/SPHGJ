# -*- coding: utf-8 -*-
"""账号名「精准取值」自检(mock 页面响应,不启动浏览器)。

被验证的链路(2026-09-25 定,替代旧的 CSS 选择器抓顶栏文本):
  1. auth/auth_data 的 data.finderUser.nickname -> captured["name"]
     同对象 finderUser.finderUsername         -> captured["finder_id"](成对,不会错位)
  2. 接口异常(缺 finderUser / errCode!=0 / 非 JSON)-> 返回空,绝不回退到页面文本
  3. auth/auth_finder_list 的 data.finderList 解析出 name + finder_id + role
  4. 兜底链:auth_data 没名字 -> 按 finder_id 反查 finder_list -> 仍无 -> finder_id 前缀占位
  5. 回归:任何场景下都不会把「切换视频号 / 取消切换」之类的控件文案当成账号名
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.login_capture import LoginSession, check_account_lock  # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)


LS_FID = "v2_060000231003b20faec8c7e18f1fc6d3cb0cee34b0773daa92bb86ba5749d1746c790f240ebc@finder"
GHOST_NAME = "切换视频号   取消切换"


class StubPage:
    """按 JS 特征分派返回值;auth_data / auth_finder_list 可分别注入。"""

    def __init__(self, auth_data=None, finder_list=None, ls_aid="AID_STUB", ls_fid=LS_FID):
        self.auth_data = auth_data          # dict | str | None(异常用 "RAW_NOT_JSON")
        self.finder_list = finder_list
        self.ls_aid = ls_aid
        self.ls_fid = ls_fid
        self.calls = []
        self.goto_urls = []

    async def evaluate(self, js, arg=None):
        if "auth_finder_list" in js:
            self.calls.append("auth_finder_list")
            if self.finder_list is None:
                raise RuntimeError("stub: no finder_list")
            return self.finder_list if isinstance(self.finder_list, str) else json.dumps(self.finder_list)
        if "auth_data" in js:
            self.calls.append("auth_data")
            if self.auth_data is None:
                raise RuntimeError("stub: no auth_data")
            return self.auth_data if isinstance(self.auth_data, str) else json.dumps(self.auth_data)
        if "finder_username" in js:          # _do_capture 第 1 步
            self.calls.append("ls_full")
            return {"aid": self.ls_aid, "finder_id": self.ls_fid}
        if "localStorage" in js:             # _read_aid
            self.calls.append("ls_aid")
            return self.ls_aid
        return ""

    async def wait_for_timeout(self, ms):
        return None

    async def goto(self, url, **kw):
        self.goto_urls.append(url)
        return None


async def _emit(ev, payload):
    return None


def new_session(page):
    s = LoginSession(None, {}, _emit, account=None)
    s.page = page
    return s


def ident_ok(nickname="示范店·甲仓", fid=LS_FID, wx="微信甲"):
    return {"errCode": 0, "errMsg": "request successful",
            "data": {"finderUser": {"nickname": nickname, "finderUsername": fid,
                                    "headImgUrl": "http://x/y"},
                     "userAttr": {"nickname": wx}}}


async def main():
    print("\n[1] _fetch_finder_identity:正常返回三值")
    s = new_session(StubPage(auth_data=ident_ok()))
    r = await s._fetch_finder_identity()
    check(r["nickname"] == "示范店·甲仓", f"nickname 解析 = {r['nickname']!r}")
    check(r["finder_id"] == LS_FID, f"finder_id 与名字同对象取出({r['finder_id'][:16]}…)")
    check(r["wx_name"] == "微信甲", f"微信昵称解析 = {r['wx_name']!r}")

    print("\n[2] _fetch_finder_identity:缺 finderUser -> 空(不回退文本)")
    s = new_session(StubPage(auth_data={"errCode": 0, "data": {"userAttr": {"nickname": "微信甲"}}}))
    r = await s._fetch_finder_identity()
    check(r["nickname"] == "" and r["finder_id"] == "", "缺 finderUser 时 name/finder_id 均为空")
    check(r["wx_name"] == "微信甲", "微信昵称仍可单独取到")

    print("\n[3] _fetch_finder_identity:errCode=300333 / 非 JSON -> 空")
    s = new_session(StubPage(auth_data={"errCode": 300333, "errMsg": "request failed"}))
    r = await s._fetch_finder_identity()
    check(r["nickname"] == "" and r["finder_id"] == "", "errCode 异常 -> 空")
    s = new_session(StubPage(auth_data="<html>not json</html>"))
    r = await s._fetch_finder_identity()
    check(r["nickname"] == "" and r["finder_id"] == "", "非 JSON -> 空(不抛异常)")

    print("\n[4] _fetch_finder_identity:无 _aid -> 直接空,不发请求")
    s = new_session(StubPage(auth_data=ident_ok(), ls_aid=""))
    r = await s._fetch_finder_identity()
    check(r["nickname"] == "", "无 aid 时返回空")
    check("auth_data" not in s.page.calls, "无 aid 时不发接口请求")

    print("\n[5] _fetch_finder_list:解析 nickname + finderUsername + roleName")
    fl = {"errCode": 0, "data": {"finderList": [
        {"nickname": "示范店·甲仓", "finderUsername": LS_FID, "roleName": "超级管理员",
         "headImgUrl": "http://a", "authImgUrl": "", "spamFlag": 0},
        {"nickname": "示范店·乙仓", "finderUsername": "FID_B", "roleName": "运营者",
         "headImgUrl": "http://b", "authImgUrl": "", "spamFlag": 1},
    ]}}
    s = new_session(StubPage(finder_list=fl))
    items = await s._fetch_finder_list()
    check(len(items) == 2, f"解析出 {len(items)} 项")
    check(items[0]["name"] == "示范店·甲仓" and items[0]["finder_id"] == LS_FID,
          "每项都带 name + finder_id(成对)")
    check(items[1]["role"] == "运营者", f"角色解析 = {items[1]['role']!r}")
    check(items[1]["_blocked"] is True, "spamFlag=1 标记为 blocked")

    print("\n[6] _fetch_finder_list:接口报错 -> []")
    s = new_session(StubPage(finder_list={"errCode": 3, "errMsg": "no permission"}))
    check(await s._fetch_finder_list() == [], "errCode!=0 -> 空列表")
    s = new_session(StubPage(finder_list=None))
    check(await s._fetch_finder_list() == [], "请求异常 -> 空列表(不抛)")

    print("\n[7] 兜底链:auth_data 正常 -> 直接用接口名")
    s = new_session(StubPage(auth_data=ident_ok(nickname="示范店·甲仓")))
    await s._do_capture()
    check(s.captured["name"] == "示范店·甲仓", f"name = {s.captured['name']!r}")
    check(s.captured["finder_id"] == LS_FID, "finder_id 取自同一对象")
    check("auth_finder_list" not in s.page.calls, "首选命中时不再调列表接口")

    print("\n[8] 兜底链:auth_data 没名字 -> auth_finder_list 按 finder_id 反查")
    s = new_session(StubPage(
        auth_data={"errCode": 0, "data": {"userAttr": {"nickname": "微信甲"}}},
        finder_list=fl))
    await s._do_capture()
    check(s.captured["name"] == "示范店·甲仓", f"反查得到 name = {s.captured['name']!r}")
    check(s.captured["finder_id"] == LS_FID, "finder_id 仍为 localStorage 值(未被误改)")

    print("\n[9] 兜底链:两个接口都失败 -> finder_id 前缀占位")
    s = new_session(StubPage(auth_data=None, finder_list=None))
    await s._do_capture()
    check(s.captured["name"] == LS_FID[:12], f"占位 = {s.captured['name']!r}")

    print("\n[10] 回归:任何场景都不得出现控件文案当名字")
    scenarios = [
        ("接口全挂", StubPage(auth_data=None, finder_list=None)),
        ("缺 finderUser", StubPage(auth_data={"errCode": 0, "data": {}})),
        ("列表为空", StubPage(auth_data={"errCode": 0, "data": {}},
                              finder_list={"errCode": 0, "data": {"finderList": []}})),
    ]
    for tag, pg in scenarios:
        s = new_session(pg)
        await s._do_capture()
        nm = s.captured.get("name") or ""
        check("切换视频号" not in nm and "取消切换" not in nm,
              f"{tag}: name={nm!r} 不含控件文案")

    print("\n[11] 回归:名字为空时不再走 DOM 文本抓取(旧实现已删)")
    import backend.login_capture as lc
    check(not hasattr(lc.LoginSession, "_capture_name"), "LoginSession 已无 _capture_name")
    import backend.selectors as sl
    check(not hasattr(sl, "ACCOUNT_NAME_CANDIDATES"), "selectors 已无 ACCOUNT_NAME_CANDIDATES")

    print("\n[12] 联动:接口名可正确参与账号锁定校验")
    nm = "示范店·甲仓"
    check(check_account_lock({"locked_finder_id": LS_FID, "locked_name": nm},
                             {"finder_id": LS_FID, "name": nm}) == "",
          "同 finder_id + 同接口名 -> 放行")
    check(check_account_lock({"locked_finder_id": "", "locked_name": nm},
                             {"finder_id": LS_FID, "name": GHOST_NAME}) != "",
          "接口名与锁定名不符 -> 拒绝")

    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for m in FAIL:
            print("  FAILED:", m)
        sys.exit(1)


asyncio.run(main())
