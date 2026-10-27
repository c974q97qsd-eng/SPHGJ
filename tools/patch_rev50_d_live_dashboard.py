# -*- coding: utf-8 -*-
"""rev50 步骤4: live_fetcher 数据大屏改 HTTP 直连（P1）。

_dashboard_post 原本必须在 liveBuild 页面里 page.evaluate(fetch) —— 三个接口
(conv / dist / refund) 每轮 60 秒各一次，全都要过一遍跨进程 IPC。
改成 wx_http 直连后不需要页面在场；page.evaluate 保留为回退。

顺带修掉 _ensure_ids 里一个多余依赖：_aid / _log_finder_id 本来就在 config 账号里
(每次登录写回)，原实现却去读 liveBuild 的 localStorage —— 页面没起来就读不到。
"""
import io
import sys

P = r"D:\SPHGJ\backend\live_fetcher.py"

s = io.open(P, encoding="utf-8").read()
orig = len(s)

# ---------- 1) import ----------
OLD_IMP = '''from .selectors import LIVE_URL
from .metrics import extract_all, shrink_conv'''
NEW_IMP = '''from .selectors import LIVE_URL
from .metrics import extract_all, shrink_conv
from . import wx_http'''
assert s.count(OLD_IMP) == 1, "import 段未命中"
s = s.replace(OLD_IMP, NEW_IMP, 1)

# ---------- 2) 常量 ----------
OLD_CONST = 'DASHBOARD_PAGE_URL_ENC = "https%3A%2F%2Fchannels.weixin.qq.com%2Fmicro%2Fstatistic%2FdashboardV4"'
NEW_CONST = ('DASHBOARD_PAGE_URL = "https://channels.weixin.qq.com/micro/statistic/dashboardV4"\n'
             'DASHBOARD_PAGE_URL_ENC = "https%3A%2F%2Fchannels.weixin.qq.com%2Fmicro%2Fstatistic%2FdashboardV4"')
assert s.count(OLD_CONST) == 1, "常量未命中"
s = s.replace(OLD_CONST, NEW_CONST, 1)

# ---------- 3) __init__ 收 account ----------
OLD_INIT = '''    def __init__(self, page, account_id, learned=None):'''
NEW_INIT = '''    def __init__(self, page, account_id, learned=None, account=None):'''
assert s.count(OLD_INIT) == 1, "__init__ 签名未命中"
s = s.replace(OLD_INIT, NEW_INIT, 1)

OLD_INIT2 = '''        self.page = page
        self.account_id = account_id'''
NEW_INIT2 = '''        self.page = page
        self.account_id = account_id
        # rev50: 账号 dict（含 _aid / _log_finder_id / storage_state）。给了就让 dashboard
        # 走纯 HTTP，不必等页面渲染；不给则只能读 localStorage（浏览器路径）。
        self._account = account or {}'''
assert s.count(OLD_INIT2) == 1, "__init__ 赋值未命中"
s = s.replace(OLD_INIT2, NEW_INIT2, 1)

# ---------- 4) _ensure_ids 优先用账号里已有的 ----------
OLD_IDS = '''    async def _ensure_ids(self):
        """从 liveBuild page localStorage 读 _aid + _log_finder_id。"""
        if self._aid and self._log_finder_id:
            return
        try:'''
NEW_IDS = '''    async def _ensure_ids(self):
        """确定 _aid + _log_finder_id。

        rev50: 优先用账号 dict 里已经存好的值 —— 这两个值每次登录就写回 config，
        不需要再去 liveBuild 的 localStorage 读。原实现把"页面在场"变成了
        dashboard 抓取的前置条件，纯属自找麻烦。
        """
        if self._aid and self._log_finder_id:
            return
        acc = self._account or {}
        if not self._aid:
            self._aid = (acc.get("_aid") or "").strip() or None
        if not self._log_finder_id:
            self._log_finder_id = (acc.get("_log_finder_id") or "").strip() or None
        if self._aid and self._log_finder_id:
            return
        if self.page is None:
            return
        try:'''
assert s.count(OLD_IDS) == 1, "_ensure_ids 未命中"
s = s.replace(OLD_IDS, NEW_IDS, 1)

# ---------- 5) _dashboard_post 先 HTTP ----------
OLD_DASH = '''    async def _dashboard_post(self, url, body):
        """在 liveBuild page 同源 fetch dashboard API(POST,cookie 自动带)。"""
        await self._ensure_ids()
        if not self._aid or not self._log_finder_id:
            return None
        full_url = f"{url}?_aid={self._aid}&_pageUrl={DASHBOARD_PAGE_URL_ENC}"
        payload = {
            "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "",
            "_log_finder_id": self._log_finder_id,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
            **body,
        }
        js = """async (args) => {
            const r = await fetch(args.url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/json'}, body: JSON.stringify(args.body)});
            return await r.text();
        }"""
        # rev21: 超时闸(页面卡死时 evaluate 永不返回 -> fetch_dashboard_data 整轮悬挂)
        txt = await _with_timeout(
            self.page.evaluate(js, {"url": full_url, "body": payload}), _EVAL_TIMEOUT)
        if not txt:
            return None
        try:
            return json.loads(txt)
        finally:'''

NEW_DASH = '''    async def _dashboard_post(self, url, body):
        """取 dashboard 数据。

        rev50: 先走进程内 HTTP(不需要 live_page、不用等 SPA 渲染)，失败回退页面 fetch。
        这三个接口是纯 JSON POST，与浏览器路径唯一的差别是少了跨进程 IPC。
        """
        await self._ensure_ids()
        if not self._aid or not self._log_finder_id:
            return None
        full_url = f"{url}?_aid={self._aid}&_pageUrl={DASHBOARD_PAGE_URL_ENC}"
        payload = {
            "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "",
            "_log_finder_id": self._log_finder_id,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
            **body,
        }
        # ---------- 1) HTTP 直连 ----------
        if self._account:
            try:
                j = await wx_http.post_json(
                    self._account, full_url, payload,
                    page_url=DASHBOARD_PAGE_URL, timeout=_EVAL_TIMEOUT)
                if isinstance(j, dict) and not j.get("__err") \
                        and j.get("errCode") in (0, None):
                    return j
                logger.debug(f"[live:{self.account_id}] dashboard HTTP 不可用: "
                             f"{j.get('__err') or ('errCode=' + str(j.get('errCode')))
                                if isinstance(j, dict) else j}")
            except Exception as e:
                logger.debug(f"[live:{self.account_id}] dashboard HTTP 异常: {e}")
        # ---------- 2) 页面 fetch 回退 ----------
        if self.page is None:
            return None
        js = """async (args) => {
            const r = await fetch(args.url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/json'}, body: JSON.stringify(args.body)});
            return await r.text();
        }"""
        # rev21: 超时闸(页面卡死时 evaluate 永不返回 -> fetch_dashboard_data 整轮悬挂)
        txt = await _with_timeout(
            self.page.evaluate(js, {"url": full_url, "body": payload}), _EVAL_TIMEOUT)
        if not txt:
            return None
        try:
            return json.loads(txt)
        finally:'''
assert s.count(OLD_DASH) == 1, "_dashboard_post 未命中"
s = s.replace(OLD_DASH, NEW_DASH, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------- 自校验 ----------
chk = io.open(P, encoding="utf-8").read()
must = [
    "from . import wx_http",
    'DASHBOARD_PAGE_URL = "https://channels.weixin.qq.com/micro/statistic/dashboardV4"',
    "def __init__(self, page, account_id, learned=None, account=None):",
    "self._account = account or {}",
    "self._account, full_url, payload",
    "# ---------- 2) 页面 fetch 回退 ----------",
]
bad = [m for m in must if m not in chk]
if bad:
    print("SELFCHECK FAIL:", bad)
    sys.exit(1)
print("OK  %d -> %d 字节 (+%d)" % (orig, len(chk), len(chk) - orig))
print("SELFCHECK PASS")
