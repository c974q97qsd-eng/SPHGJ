# -*- coding: utf-8 -*-
"""rev72 一次性补丁:评论写操作默认纯 HTTP + 账号隔离并发。

按铁律 5:整文件读入 -> 逐条 assert 唯一命中 -> 一次写回 -> 自校验。
用法: python tools/patch_rev72.py          应用补丁
      python tools/patch_rev72.py --verify  纯校验(不写盘)
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY_ONLY = "--verify" in sys.argv

AC = "backend/api_client.py"
AM = "backend/account_manager.py"
SV = "backend/server.py"


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def write(rel, s):
    io.open(os.path.join(ROOT, rel), "w", encoding="utf-8", newline="").write(s)


def apply(rel, patches):
    s = read(rel)
    for old, new, tag in patches:
        n = s.count(old)
        assert n == 1, "%s 锚点 %s 命中 %d 次(应为 1)" % (rel, tag, n)
        s = s.replace(old, new, 1)
    return s


# ==================== api_client.py ====================
AC_PATCHES = [
    # 1) import threading
    ("import logging\nimport uuid\n",
     "import logging\nimport threading\nimport uuid\n",
     "import threading"),

    # 2) 模块级账号锁表 + account_lock()（按事件循环隔离，跨 api 实例重建共享）
    ("class WxApiClient:\n",
     '''# ==================== rev72: 账号隔离并发锁 ====================
# 每个账号一把 asyncio.Lock:同一账号的所有 _post 严格串行(节流/退避/每日计数
# 才不会被并发打穿),不同账号各持一把,互不阻塞 -> 真正并行。
# 锁放模块级而非实例级:worker 开关浏览器时会重建 WxApiClient(4 处),实例级锁
# 在重建瞬间会出现两把锁,同账号仍可能并发发包。
# key = (事件循环 id, 账号标识):asyncio.Lock 绑定 loop,测试和重启不能复用旧锁。
_ACCOUNT_LOCKS = {}
_ACCOUNT_LOCKS_GUARD = threading.Lock()


def account_lock(key):
    """按 (事件循环, 账号标识) 返回同一把 asyncio.Lock,永不抛。"""
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    k = (loop_id, str(key or ""))
    with _ACCOUNT_LOCKS_GUARD:
        lk = _ACCOUNT_LOCKS.get(k)
        if lk is None:
            lk = asyncio.Lock()
            _ACCOUNT_LOCKS[k] = lk
        return lk


def reset_account_locks_for_test():
    with _ACCOUNT_LOCKS_GUARD:
        _ACCOUNT_LOCKS.clear()


class WxApiClient:
''',
     "账号锁表"),

    # 3) 模块 docstring 说明
    ("rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py),写接口默认仍走浏览器。",
     "rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py)。\n"
     "rev72: 写接口(回复/删除/置顶/隐藏)也默认纯 HTTP,不再为写操作开浏览器。\n"
     "       账号隔离:同账号串行、多账号并行,见 account_lock()。",
     "模块 docstring"),

    # 4) 默认写通道改 http + 锁维度配置
    ('        self._write_via = str(rc.get("api_write_via") or "browser").lower()\n',
     '        # rev72: 写操作与读一致,默认纯 HTTP。config 显式 "browser" 可整体回退。\n'
     '        self._write_via = str(rc.get("api_write_via") or "http").lower()\n'
     '        # 账号隔离锁维度:account(登录卡片,默认) / finder(视频号)\n'
     '        self._write_lock_scope = str(rc.get("write_lock_scope") or "account").lower()\n',
     "默认写通道"),

    # 5) 新增 _lock_key()
    ("    def _is_write(self, path):\n        return any(w in path for w in self.WRITE_PATHS)\n",
     "    def _is_write(self, path):\n        return any(w in path for w in self.WRITE_PATHS)\n\n"
     "    def _lock_key(self):\n"
     '        """rev72: 账号隔离锁的标识。默认按登录卡片 id(与每日写计数同维度);\n'
     '        risk_control.write_lock_scope="finder" 可改成按视频号串行。\n'
     '        """\n'
     '        if self._write_lock_scope == "finder":\n'
     '            return str(self.finder_id or self.account_id or self.aid or "anon")\n'
     '        return str(self.account_id or self.finder_id or self.aid or "anon")\n',
     "_lock_key"),

    # 6) _post 包装账号锁，原实现改名 _post_impl
    ("    async def _post(self, path, body, base=None, page_url=None):\n"
     "        # 写操作每日上限:超限跳过,不调 API(不计退避)\n",
     "    async def _post(self, path, body, base=None, page_url=None):\n"
     "        # rev72: 账号隔离 —— 同账号严格串行,不同账号并行。\n"
     "        # 锁按 (loop, 账号) 存模块级表,api 实例重建后仍是同一把。\n"
     "        async with account_lock(self._lock_key()):\n"
     "            return await self._post_impl(path, body, base, page_url)\n\n"
     "    async def _post_impl(self, path, body, base=None, page_url=None):\n"
     "        # 写操作每日上限:超限跳过,不调 API(不计退避)\n",
     "_post 包装"),

    # 7) 失败豁免集合加 http_blocked
    ('        _local_err = {"no_cookie", "eval_error", "eval_timeout", "no_page"}\n',
     '        # rev72: http_blocked = 纯 HTTP 写模式在 block 期的零发包短路,\n'
     '        # 不是服务端拒绝,不能触发 30/120/600s 账号级退避。\n'
     '        _local_err = {"no_cookie", "eval_error", "eval_timeout", "no_page",\n'
     '                      "http_blocked"}\n',
     "退避豁免"),

    # 8) _post_impl 回退分支:纯 HTTP 写不回退浏览器
    ("        if resp is None:\n            if not self._page_alive():\n",
     "        if resp is None:\n"
     "            if is_write and self._write_via == \"http\":\n"
     "                # rev72: 纯 HTTP 写模式不存在「回退浏览器」。no_cookie 表示\n"
     "                # 请求根本没发出去,直接报本地错误(豁免退避),零发包。\n"
     "                resp = {\"__err\": \"no_cookie\"}\n"
     "            elif not self._page_alive():\n",
     "不回退浏览器"),

    # 9) _via_http:纯 HTTP 写不受 block / 页面状态影响
    ("        if time.time() < self._http_block_until:\n            return False\n"
     "        # rev54: 这一轮已经撞到 300333/300330(cookie 已死),后面别再逐个请求去撞\n"
     "        if not is_write and self.auth_fail:\n            return False\n"
     "        if not is_write:\n            return self._read_via == \"http\"\n"
     "        if self._write_via == \"http\":\n            return True\n",
     "        # rev72: 纯 HTTP 写模式下 block 期也不能回退浏览器 —— 浏览器可能\n"
     "        # 根本没开(或已死),回退只会拿到 no_page/eval_error。改由\n"
     "        # _post_via_http 短路返回本地错误,零发包。\n"
     "        _pure_http_write = is_write and self._write_via == \"http\"\n"
     "        if time.time() < self._http_block_until and not _pure_http_write:\n"
     "            return False\n"
     "        # rev54: 这一轮已经撞到 300333/300330(cookie 已死),后面别再逐个请求去撞\n"
     "        if not is_write and self.auth_fail:\n            return False\n"
     "        if not is_write:\n            return self._read_via == \"http\"\n"
     "        # rev72: 写操作默认纯 HTTP(与读一致),不再看页面是否活着。\n"
     "        if self._write_via == \"http\":\n            return True\n",
     "_via_http 写分支"),

    # 10) _post_via_http 里补 block 零发包短路
    ("        try:\n            acc = self._latest_account()\n"
     "            # _aid 每次登录都会变;URL 是早先拼的,这里按最新值纠正\n",
     "        # rev72: 纯 HTTP 写模式下 _via_http 会无视 block 直接进来,\n"
     "        # 这里补上零发包短路,避免拿已死的 cookie 白撞一枪。\n"
     "        if time.time() < self._http_block_until:\n"
     '            return self._http_give_up("blocked", path, is_write=is_write)\n'
     "        try:\n            acc = self._latest_account()\n"
     "            # _aid 每次登录都会变;URL 是早先拼的,这里按最新值纠正\n",
     "HTTP block 短路"),
]


# ==================== account_manager.py ====================
AM_PATCHES = [
    # 1) worker 新增 _write_via_http() + hold_for_manual 纯 HTTP 早退
    ("    async def hold_for_manual(self, delay=None):\n"
     '        """手动操作(回复/删除/置顶)期间保持浏览器,操作完成后延迟回收省内存。\n'
     "\n"
     "        - 首次调用:经引用计数开浏览器(只 +1),开进程;\n"
     "        - delay 内再次调用(连回复数条):_manual_hold 已置位,不再重复开进程,只续期计时;\n"
     "        - 计时到期:释放这一份引用并回收(非直播且无其它引用时关 context)。\n"
     "        与抓取/直播的引用计数相互独立(同一把 _browser_lock 串行),互不干扰。\n"
     "\n"
     "        delay 默认 120s,可经 config.manual_release_delay_sec 调整。\n"
     '        """\n'
     "        if delay is None:\n",
     "    def _write_via_http(self):\n"
     '        """rev72: 写操作是否走纯 HTTP(口径与 api_client 默认值一致)。"""\n'
     "        rc = (self.config or {}).get(\"risk_control\") or {}\n"
     '        return str(rc.get("api_write_via") or "http").lower() == "http"\n'
     "\n"
     "    async def hold_for_manual(self, delay=None):\n"
     '        """手动操作(回复/删除/置顶)期间保持浏览器,操作完成后延迟回收省内存。\n'
     "\n"
     "        rev72: 纯 HTTP 写模式**不再为写操作开浏览器** —— 不发页面内 fetch,\n"
     "        开 Chromium 纯属浪费,而且浏览器进程已死时会把写操作带崩(见 rev71)。\n"
     "        早退不碰 _browser_lock / _browser_refs / 延迟回收计时器。\n"
     "        api_write_via=\"browser\" 时完全恢复原来的行为。\n"
     "\n"
     "        - browser 模式:首次调用经引用计数开浏览器(只 +1);\n"
     "        - delay 内再次调用:_manual_hold 已置位,只续期计时;\n"
     "        - 计时到期:释放这一份引用并回收(非直播且无其它引用时关 context)。\n"
     '        """\n'
     "        if self._write_via_http():\n"
     "            return self.logged_in\n"
     "        if delay is None:\n",
     "hold 纯 HTTP 早退"),

    # 2) _fetch_loop docstring
    ('        - 每 fetch_interval_sec(默认 600s = 10 分钟)抓一次;多账号经 _cycle_offset 错峰\n'
     '          + 全局 _fetch_lock 串行,形成"间隔排队":任意时刻最多 1 个账号在抓,\n'
     "          浏览器不并发常驻。\n",
     "        - 每 fetch_interval_sec(默认 600s = 10 分钟)抓一次;多账号经 _cycle_offset 错峰。\n"
     "          rev72: _fetch_lock 改成每账号一把,不同账号并行。读接口早已纯 HTTP,\n"
     "          整轮持全局锁只会把自动回复/自动删除也一起跨账号串行化。\n"
     "          risk_control.fetch_parallel=false 可恢复原来的全局串行。\n",
     "fetch_loop docstring"),

    # 3) 抓取锁注释
    ("            # 串行抓取:全局队列锁,保证多账号不同时拉起浏览器(省内存 + 防风控关联)\n",
     "            # rev72: 账号隔离抓取锁(同账号串行、跨账号并行)\n",
     "抓取锁注释"),

    # 4) manager 新增 _account_fetch_lock()
    ("    def get_login_session(self, sid):\n",
     "    def _account_fetch_lock(self, account_id):\n"
     '        """rev72: 评论抓取按账号隔离 —— 不同账号并行,同账号由自己的循环串行。\n'
     "\n"
     "        原来 manager 把唯一一把 asyncio.Lock 注入所有 worker,整轮抓取(含\n"
     "        自动回复/自动删除)全部跨账号串行;HTTP 直连时代这把锁成了纯人为瓶颈。\n"
     "        risk_control.fetch_parallel=false 恢复原全局串行(多账号同时开 Chromium\n"
     "        内存吃紧时的回退通道)。\n"
     '        """\n'
     "        rc = (self.config.get(\"risk_control\") or {})\n"
     '        if str(rc.get("fetch_parallel", "true")).lower() in ("false", "0", "no"):\n'
     "            if self._fetch_lock is None:\n"
     "                self._fetch_lock = asyncio.Lock()\n"
     "            return self._fetch_lock\n"
     "        return asyncio.Lock()\n"
     "\n"
     "    def get_login_session(self, sid):\n",
     "manager 账号抓取锁"),

    # 5) manager.start() 里的全局锁注释
    ('        # 评论抓取全局串行锁:多账号经此锁 + _cycle_offset 错峰,形成"间隔排队",\n'
     "        # 任意时刻最多 1 个账号在抓,浏览器不并发常驻(省内存)。\n"
     "        self._fetch_lock = asyncio.Lock()\n",
     "        # rev72: 默认按账号发独立锁(见 _account_fetch_lock);这把共享锁只在\n"
     "        # risk_control.fetch_parallel=false 时作为回退通道使用。\n"
     "        self._fetch_lock = asyncio.Lock()\n",
     "start 全局锁注释"),

    # 6)(7) 两处注入点改成按账号取锁
    ("                    w._fetch_lock = self._fetch_lock\n",
     "                    w._fetch_lock = self._account_fetch_lock(acc[\"id\"])\n",
     "start 注入点"),
    ("        w._fetch_lock = self._fetch_lock\n",
     "        w._fetch_lock = self._account_fetch_lock(account_id)\n",
     "start_account 注入点"),
]


# ==================== server.py ====================
SV_PATCHES = [
    ('               "risk_control": {"read_interval": [1.0, 2.5], "write_interval": [4.0, 8.0],\n'
     '                                 "night_hours": [0, 6], "night_interval_multiplier": 3,\n'
     '                                 "daily_write_limit": 100}}\n',
     '               "risk_control": {"read_interval": [1.0, 2.5], "write_interval": [4.0, 8.0],\n'
     '                                 "api_read_via": "http", "api_write_via": "http",\n'
     '                                 "write_lock_scope": "account", "fetch_parallel": True,\n'
     '                                 "night_hours": [0, 6], "night_interval_multiplier": 3,\n'
     '                                 "daily_write_limit": 100}}\n',
     "默认配置模板"),
]


def selfcheck(s_ac, s_am, s_sv):
    checks = [
        ("AC import threading", "import threading" in s_ac),
        ("AC 账号锁表", "_ACCOUNT_LOCKS_GUARD = threading.Lock()" in s_ac),
        ("AC account_lock 按 loop", "loop_id = id(asyncio.get_running_loop())" in s_ac),
        ("AC 默认写 http", 'rc.get("api_write_via") or "http"' in s_ac),
        ("AC 无 browser 默认", 'or "browser").lower()' not in s_ac),
        ("AC _lock_key", "def _lock_key(self):" in s_ac),
        ("AC _post 包装锁", "async with account_lock(self._lock_key()):" in s_ac),
        ("AC _post_impl 存在", "async def _post_impl(self, path, body" in s_ac),
        ("AC 退避豁免 http_blocked", '"http_blocked"' in s_ac),
        ("AC 纯HTTP写不回退页面", 'resp = {"__err": "no_cookie"}' in s_ac),
        ("AC _via_http 纯写短路", "_pure_http_write = is_write" in s_ac),
        ("AC HTTP block 零发包", 'self._http_give_up("blocked", path' in s_ac),
        ("AM _write_via_http", "def _write_via_http(self):" in s_am),
        ("AM hold 早退", "if self._write_via_http():" in s_am
         and "return self.logged_in" in s_am),
        ("AM 账号抓取锁", "def _account_fetch_lock(self, account_id):" in s_am),
        ("AM 两处按账号注入", s_am.count("self._account_fetch_lock(") == 2),
        ("AM fetch_parallel 回退", 'rc.get("fetch_parallel"' in s_am),
        ("SV 默认模板", '"api_write_via": "http"' in s_sv
         and '"fetch_parallel": True' in s_sv),
    ]
    ok = True
    for name, cond in checks:
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and bool(cond)
    for rel, s in ((AC, s_ac), (AM, s_am), (SV, s_sv)):
        try:
            compile(s, rel, "exec")
            print("PASS compile " + rel)
        except SyntaxError as e:
            print("FAIL compile %s: %s" % (rel, e))
            ok = False
    print("\nSELFCHECK: " + ("PASS" if ok else "FAIL"))
    return ok


def main():
    s_ac = read(AC) if VERIFY_ONLY else apply(AC, AC_PATCHES)
    s_am = read(AM) if VERIFY_ONLY else apply(AM, AM_PATCHES)
    s_sv = read(SV) if VERIFY_ONLY else apply(SV, SV_PATCHES)
    if not VERIFY_ONLY:
        write(AC, s_ac)
        write(AM, s_am)
        write(SV, s_sv)
        print("已写入 3 个文件\n")
    ok = selfcheck(s_ac, s_am, s_sv)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
