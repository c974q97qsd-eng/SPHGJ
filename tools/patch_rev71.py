# -*- coding: utf-8 -*-
"""rev71 补丁:浏览器 context / page「对象还在但底层已死」的存活判断与回退。

症状(母本 2026-11-16 03:05:28 / 03:05:58):
  [api] evaluate 异常 path=comment/del_comment:
        Page.evaluate: Target page, context or browser has been closed
  -> eval_error -> 30s/120s 全局退避 -> 删除评论 502

根因:
  Chromium 进程死了,但 Playwright 的 context 对象仍在内存里。
  所有入口都只判 `self.context is None`,死 context 不会被重建;
  WxApiClient 也仍握着那个死 page,写操作默认走浏览器就必然炸。

改动(只补既有设计原则,不改业务方向):
  account_manager: _context_alive() / _drop_dead_context(),三处开浏览器入口改用活体判断
  api_client:      _page_alive(),死 page 等价「无页面」→ 写操作走 HTTP(rev62 已确立);
                   eval_error/eval_timeout 归入本地通道错误,不再触发账号级退避
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def write(rel, s):
    io.open(os.path.join(ROOT, rel), "w", encoding="utf-8", newline="").write(s)


def apply(rel, patches):
    s = read(rel)
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s 锚点 [%s] 命中 %d 次(应为 1)" % (rel, tag, n)
        s = s.replace(old, new, 1)
    compile(s, rel, "exec")
    write(rel, s)
    print("patched %s (%d 处)" % (rel, len(patches)))
    return s


AM = "backend/account_manager.py"
AC = "backend/api_client.py"

AM_PATCHES = [
    ("A1 新增活体方法 + 改 _open_context 幂等判断",
     '''    async def _open_context(self):
        """懒加载开 Chromium:建 context+评论页,登录校验,建/重绑 API 客户端。幂等。"""
        if self.context is not None:
            return
''',
     '''    # ==================== rev71: context 活体检查 ====================
    def _context_alive(self):
        """context 对象存在 != 浏览器还活着。

        Chromium 被回收/崩溃后 Playwright 仍握着 context 对象,但 pages 全部
        is_closed()。原来所有入口只判 `self.context is None`,于是死 context
        永远不会被重建:轻量探测回退开页报 `BrowserContext.new_page: Target
        page... has been closed`,手动删除拿死 page 去 evaluate 报 eval_error,
        再被计成接口失败触发 30s/120s 账号级退避。
        """
        ctx = self.context
        if ctx is None:
            return False
        try:
            pages = ctx.pages
            if not pages:
                return False
            return any(not p.is_closed() for p in pages)
        except Exception:
            # ctx 本身已废:连 .pages 都取不到
            return False

    async def _drop_dead_context(self):
        """只清引用 + 重建无浏览器 api,不对死 context 再调 close。

        不能用 _close_context():它会走 close_context_safely 的兜底
        reap_profile_orphans,按 --user-data-dir 杀残留进程,在这个时机
        有误伤刚拉起的新 Chromium 的风险。
        """
        lf = self.live_fetcher
        self.context = None
        self.page = None
        self.live_page = None
        self.live_fetcher = None
        self._verify_result = None
        if self.api is not None:
            old_api = self.api
            self.api = WxApiClient(None, self.account, self.config, self.storage)
            # 继承「cookie 已死/先别试 HTTP」的状态,否则重建会抹掉退避
            self.api.auth_fail = getattr(old_api, "auth_fail", None)
            self.api._http_block_until = getattr(old_api, "_http_block_until", 0.0)
            self.api._http_logged = getattr(old_api, "_http_logged", False)
            for comp in (self.auto_reply, self.auto_commenter, self.auto_delete, self.fetcher):
                if comp is not None:
                    comp.api = self.api
        if lf:
            try:
                await lf.close()
            except Exception:
                pass

    async def _open_context(self):
        """懒加载开 Chromium:建 context+评论页,登录校验,建/重绑 API 客户端。幂等。"""
        if self._context_alive():
            return
        if self.context is not None:
            # rev71: 对象还在但浏览器已死 —— 先清引用,下面重新拉起
            logger.warning(f"[{self.account['id']}] 浏览器 context 已失效,重建")
            await self._drop_dead_context()
'''),
    ("A2 ensure_browser 用活体判断",
     '''            self._browser_refs += 1
            if self.context is None:
                try:
                    await self._open_context()
''',
     '''            self._browser_refs += 1
            if not self._context_alive():
                try:
                    await self._open_context()
'''),
    ("A3 hold_for_manual 用活体判断",
     '''                self._browser_refs += 1
                if self.context is None:
                    try:
                        await self._open_context()
''',
     '''                self._browser_refs += 1
                if not self._context_alive():
                    try:
                        await self._open_context()
'''),
]

AC_PATCHES = [
    ("B1 新增 _page_alive",
     '''    def _is_write(self, path):
        return any(w in path for w in self.WRITE_PATHS)
''',
     '''    def _page_alive(self):
        """page 对象存在 != 页面还活着(rev71)。

        死 page 必须等价「没有页面」:否则写操作会拿它去 evaluate 报
        Target closed,而不是按 rev62 既定的原则走 HTTP 直连。
        """
        p = self.page
        if p is None:
            return False
        try:
            return not p.is_closed()
        except Exception:
            return False

    def _is_write(self, path):
        return any(w in path for w in self.WRITE_PATHS)
'''),
    ("B2 eval_error/eval_timeout 豁免账号级退避",
     '''        if err and err != "daily_write_limit" and not (
                err == "no_cookie" or str(err).startswith("no_page")):
            return True
''',
     '''        # rev71: eval_error / eval_timeout 是「浏览器通道已死」,不是服务端拒绝。
        # 原来照样计失败 -> 30/120/600s 退避是按账号的,会把同账号正常读请求
        # 一起停掉(rev62 已为 no_page 修过同一类问题)。注意:failed() 仍会
        # 判它们失败,上层不会误报成功。
        _local_err = {"no_cookie", "eval_error", "eval_timeout", "no_page"}
        if err and err != "daily_write_limit" and not (
                err in _local_err or str(err).startswith("no_page")):
            return True
'''),
    ("B3 _post 回退分支用活体判断",
     '''            if self.page is None:
                # rev62: 没有页面就不存在"回退浏览器"这件事。原来这里一律调
                # _post_via_page,拿回来的 no_page 既看不出真实原因(no_cookie?
                # cookie 已过期?),又会被计成一次接口失败去触发退避。
                resp = {"__err": "no_page:" + (self._http_last_why or "unknown")}
''',
     '''            if not self._page_alive():
                # rev62: 没有页面就不存在"回退浏览器"这件事。原来这里一律调
                # _post_via_page,拿回来的 no_page 既看不出真实原因(no_cookie?
                # cookie 已过期?),又会被计成一次接口失败去触发退避。
                # rev71: 页面对象还在但已关闭,同样算「没有页面」。
                resp = {"__err": "no_page:" + (self._http_last_why or "unknown")}
'''),
    ("B4 _via_http 死 page 等价无页面",
     '''        if self.page is None:
            return True
''',
     '''        # rev71: 死 page 等价「没有页面」 —— 否则写操作仍会拿死对象去
        # evaluate,报 Target closed 并触发 30/120s 全局退避。
        if not self._page_alive():
            return True
'''),
    ("B5 _post_via_page 死 page 直接返回 no_page",
     '''        if self.page is None:
            # rev52: 纯 HTTP 模式(未开浏览器)下的回退请求。不能抛异常 ——
''',
     '''        if not self._page_alive():
            # rev52/rev71: 纯 HTTP 模式 / 页面已死,都不存在"回退浏览器"这件事。
            # 不能抛异常 ——
'''),
]

VERIFY_ONLY = "--verify" in sys.argv
s_am = read(AM) if VERIFY_ONLY else apply(AM, AM_PATCHES)
s_ac = read(AC) if VERIFY_ONLY else apply(AC, AC_PATCHES)

# ---------------- 自校验 ----------------
checks = [
    ("AM 新增 _context_alive", "def _context_alive(self):" in s_am),
    ("AM 新增 _drop_dead_context", "async def _drop_dead_context(self):" in s_am),
    ("AM _open_context 用活体", "if self._context_alive():\n            return" in s_am),
    ("AM 三处开浏览器入口用活体", s_am.count("if not self._context_alive():") == 2
     and "if self._context_alive():" in s_am),
    ("AM 不再有裸 context is None 开浏览器", 'if self.context is None:\n                try:\n                    await self._open_context()' not in s_am),
    ("AC 新增 _page_alive", "def _page_alive(self):" in s_ac),
    ("AC _via_http 用活体", "if not self._page_alive():\n            return True" in s_ac),
    ("AC _post 用活体", 'if not self._page_alive():\n                # rev62' in s_ac),
    ("AC _post_via_page 用活体", "if not self._page_alive():\n            # rev52/rev71" in s_ac),
    ("AC 豁免 eval_error", '"eval_error", "eval_timeout"' in s_ac),
    ("AC failed() 未改(仍判 __err 失败)", "if resp.get(\"__err\"):\n            return True" in s_ac),
    ("AM compile", True),
    ("AC compile", True),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("SELFCHECK:", "PASS" if not bad else "FAIL " + str(bad))
sys.exit(0 if not bad else 1)
