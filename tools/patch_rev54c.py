# -*- coding: utf-8 -*-
"""rev54c:关 context 时把 API 客户端换回「无浏览器」版本。

线上现象(重启后 04:16:25 一条):
    WARNING [api] evaluate 异常 path=comment/comment_list:
            Page.evaluate: Target page, context or browser has been closed
    WARNING [api] 请求失败(连续 1 次),退避 30s

根因:release_idle_browser / _close_context 关掉 context 之后,self.api 仍然握着
那个已经死掉的 page。于是任何「这一次 HTTP 走不通要回退页面」的请求,都会拿着死
page 去 evaluate —— 必然 Target closed,而且被记成一次失败触发 30s 退避。
rev52 起 _http_block_until 会把 HTTP 挡 180s,这段时间每一轮都直接走 page,
噪音反而更密。

修法:_close_context 里重建一个 page=None 的客户端并重绑到各组件;同时把
auth_fail / _http_block_until / _http_logged 从旧实例继承过来 ——
否则重建会把「cookie 已死,先别试」的退避状态一起抹掉。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AM = os.path.join(ROOT, "backend", "account_manager.py")

OLD = '''    async def _close_context(self):
        """关 context 收回 Chromium 进程(兜底 kill 残留 chrome)。清所有页面引用。"""
        ctx = self.context
        lf = self.live_fetcher
        self.context = None
        self.page = None
        self.live_page = None
        self.live_fetcher = None
'''

NEW = '''    async def _close_context(self):
        """关 context 收回 Chromium 进程(兜底 kill 残留 chrome)。清所有页面引用。"""
        ctx = self.context
        lf = self.live_fetcher
        self.context = None
        self.page = None
        self.live_page = None
        self.live_fetcher = None
        # rev54c: page 已经死了,但 self.api 还握着它 —— 后面任何「这次 HTTP 走不通、
        # 回退页面 fetch」的请求都会拿着死 page 去 evaluate,报 Target closed,
        # 还会被记成一次失败触发 30s 退避。这里换回无浏览器客户端,等 _open_context 重建。
        # auth_fail / _http_block_until 必须继承:否则「cookie 已死,先别试 HTTP」的
        # 退避状态会被重建抹掉,下一轮又拿着死 cookie 去撞 300333。
        if self.api is not None:
            old_api = self.api
            self.api = WxApiClient(None, self.account, self.config, self.storage)
            self.api.auth_fail = getattr(old_api, "auth_fail", None)
            self.api._http_block_until = getattr(old_api, "_http_block_until", 0.0)
            self.api._http_logged = getattr(old_api, "_http_logged", False)
            for comp in (self.auto_reply, self.auto_commenter, self.auto_delete, self.fetcher):
                if comp is not None:
                    comp.api = self.api
'''


def main():
    with io.open(AM, encoding="utf-8") as f:
        s = f.read()
    if "rev54c" in s:
        print("已打过")
        return 0
    if s.count(OLD) != 1:
        print("锚点命中 %d 次(要求 1)" % s.count(OLD))
        return 1
    with io.open(AM, "w", encoding="utf-8", newline="") as f:
        f.write(s.replace(OLD, NEW, 1))
    print("已应用")

    with io.open(AM, encoding="utf-8") as f:
        s2 = f.read()
    checks = [
        "rev54c",
        'self.api = WxApiClient(None, self.account, self.config, self.storage)',
        'self.api.auth_fail = getattr(old_api, "auth_fail", None)',
        'self.api._http_block_until = getattr(old_api, "_http_block_until", 0.0)',
        "comp.api = self.api",
    ]
    bad = [c for c in checks if c not in s2]
    if bad:
        for c in bad:
            print("  SELFCHECK FAIL:", c)
        return 1
    print("SELFCHECK PASS (%d 项)" % len(checks))
    import py_compile
    py_compile.compile(AM, doraise=True)
    print("语法检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
