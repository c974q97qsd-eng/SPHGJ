# -*- coding: utf-8 -*-
"""rev54b:rev54 的两处收尾修补。

1. 掉线判定补全证据链:已有 context 时 ensure_browser 不会重新做 auth 硬验,
   拿的是旧的 logged_in=True —— 登录态半路失效的账号会一直"看起来登录着"反复失败,
   永远进不了离线判定。这里在 HTTP 明确报过登录态失效之后主动重验一次。
2. 掉线后排队的扫码启动不要卡在抓取锁里(它要 launch 浏览器,几秒钟)。
3. 手动「立即抓取」撞到离线账号时给一条可见日志,别让人以为抓了 0 条。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AM = os.path.join(ROOT, "backend", "account_manager.py")

PATCHES = []


def P(old, new, tag):
    PATCHES.append((old, new, tag))


P('''        if self.auth_state == "offline":
            return None, False
        # 1) 纯 HTTP:不需要任何 Chromium 进程
''', '''        if self.auth_state == "offline":
            logger.info(f"[{self.account['id']}] 登录已失效(待扫码),本轮不发任何请求")
            return None, False
        # 1) 纯 HTTP:不需要任何 Chromium 进程
''', "am:offline_log")

P('''        # 2) 浏览器回退
        ok = await self.ensure_browser()
''', '''        # rev54: context 已经开着时 ensure_browser 不会重做 auth 硬验,拿的是上次的
        # logged_in。登录态半路失效的账号会「看起来还登录着」然后整轮失败,永远进不了
        # 离线判定。这里在 HTTP 明确报过 300333/300330 之后主动重验一次(证据链补全)。
        if self.context is not None and self.api is not None and getattr(self.api, "auth_fail", None):
            try:
                self.logged_in = await self._verify_login(wait_sec=5.0)
                self._verify_result = "ok" if self.logged_in else "no"
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 复用 context 补验登录态异常: {e}")
        # 2) 浏览器回退
        ok = await self.ensure_browser()
''', "am:reverify")

P('''            # 自动推网页二维码:与启动时未登录账号走同一条串行队列,不会同时弹多个
            try:
                await self.enqueue_auto_relogin(account_id)
            except Exception as e:
                logger.warning(f"[{account_id}] 掉线后排队扫码失败: {e}")
''', '''            # 自动推网页二维码:与启动时未登录账号走同一条串行队列,不会同时弹多个。
            # 用 create_task:扫码要 launch 浏览器(几秒),别让它卡在调用方持有的抓取锁里。
            asyncio.create_task(self._queue_relogin_after_offline(account_id))
''', "am:queue_task")

P('''    async def _on_worker_auth(self, account_id, state, reason=""):
''', '''    async def _queue_relogin_after_offline(self, account_id):
        try:
            await self.enqueue_auto_relogin(account_id)
        except Exception as e:
            logger.warning(f"[{account_id}] 掉线后排队扫码失败: {e}")

    async def _on_worker_auth(self, account_id, state, reason=""):
''', "am:queue_helper")


def apply():
    changed = 0
    for old, new, tag in PATCHES:
        with io.open(AM, encoding="utf-8") as f:
            s = f.read()
        if new in s:
            print(f"  [skip] {tag} 已打过")
            continue
        n = s.count(old)
        if n != 1:
            print(f"  [FAIL] {tag} 命中 {n} 次(要求 1 次)")
            return False
        with io.open(AM, "w", encoding="utf-8", newline="") as f:
            f.write(s.replace(old, new, 1))
        changed += 1
        print(f"  [ok] {tag}")
    print(f"共应用 {changed} 处")
    return True


def selfcheck():
    checks = [
        "本轮不发任何请求",
        "self.logged_in = await self._verify_login(wait_sec=5.0)",
        "asyncio.create_task(self._queue_relogin_after_offline(account_id))",
        "async def _queue_relogin_after_offline(self, account_id):",
    ]
    with io.open(AM, encoding="utf-8") as f:
        s = f.read()
    bad = [c for c in checks if c not in s]
    for c in bad:
        print(f"  [SELFCHECK FAIL] {c}")
    if not bad:
        print(f"  SELFCHECK PASS ({len(checks)} 项)")
    return not bad


if __name__ == "__main__":
    print("rev54b 收尾补丁")
    if not apply():
        sys.exit(1)
    if not selfcheck():
        sys.exit(1)
    import py_compile
    py_compile.compile(AM, doraise=True)
    print("语法检查通过")
