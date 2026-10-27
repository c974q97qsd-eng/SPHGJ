# -*- coding: utf-8 -*-
"""rev47 patch 7: 周期性完整检测走"快速确认"

新发现(02:03~02:05 实测 + DOM dump):
  LIVE_URL = /platform/live/liveBuild,但未开播时 goto 完落地 URL 是 /platform/ ——
  直播控制台根本没打开(SPA 直接弹回首页)。所以未开播时:
    · 页面不会请求 get_live_info -> liveStats 永远为空(这不是检测失灵)
    · 页面上也没有"未开播/直播中"文案 -> DOM 判据恒为"未知"
  而"完整检测"仍要为此白等 20s + reload 12s,一次 140 秒。

安全前提:这次快速确认只在【刚做完轻量探测且明确未发现信号】时启用。
真的在播时 check_live_status 会带 audiencePlayUrl(历史在播场次都是这么拿到流的),
轻量探测会先命中,不会走到这条路 —— 因此不会漏掉真直播。
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = [
    (
        '''    async def _open_and_detect_live(self):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。''',
        '''    async def _open_and_detect_live(self, fast=False):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。

        fast=True:轻量探测已明确"没信号"时的快速确认 —— 等 4 秒即可,不做 reload 重试。
        依据:未开播时直播控制台会被 SPA 弹回 /platform/,页面不会请求 get_live_info,
        再怎么等也等不到 liveStats(实测 02:03~02:05)。真在播时轻量探测会先命中,
        根本走不到这里,所以不会有假阴性。''',
        "sig",
    ),
    (
        '''            info = await self._wait_for_live_detection(lf, timeout=20.0)''',
        '''            info = await self._wait_for_live_detection(
                lf, timeout=4.0 if fast else 20.0, allow_relearn=not fast)''',
        "call",
    ),
    (
        '''    async def _wait_for_live_detection(self, lf, timeout=20.0):''',
        '''    async def _wait_for_live_detection(self, lf, timeout=20.0, allow_relearn=True):''',
        "wsig",
    ),
    (
        '''        try:
            if await lf.relearn():''',
        '''        if not allow_relearn:
            return info or {}
        try:
            if await lf.relearn():''',
        "relearn",
    ),
    (
        '''    async def _open_full(self):
        """完整检测(开直播大屏页)。探测器可能在没有浏览器时调用,这里负责补引用。"""
        if self.context is not None:
            return await self._open_and_detect_live()
        if not self.ensure_browser_scalar:
            pass
        if not await self.ensure_browser():
            logger.debug(f"[{self.account['id']}] 完整检测跳过:开浏览器失败")
            return False
        try:
            return await self._open_and_detect_live()
        finally:
            await self.release_idle_browser()''',
        '''    async def _open_full(self, fast=False):
        """完整检测(开直播大屏页)。探测器可能在没有浏览器时调用,这里负责补引用。"""
        if self.context is not None:
            return await self._open_and_detect_live(fast=fast)
        if not await self.ensure_browser():
            logger.debug(f"[{self.account['id']}] 完整检测跳过:开浏览器失败")
            return False
        try:
            return await self._open_and_detect_live(fast=fast)
        finally:
            await self.release_idle_browser()''',
        "openfull",
    ),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    if tag == "openfull":
        # 该段可能因上一处补丁而略有差异,按小步替换处理
        if s.count(old) != 1:
            old = '''    async def _open_full(self):
        """完整检测(开直播大屏页)。探测器可能在没有浏览器时调用,这里负责补引用。"""
        if self.context is not None:
            return await self._open_and_detect_live()
        if not await self.ensure_browser():
            logger.debug(f"[{self.account['id']}] 完整检测跳过:开浏览器失败")
            return False
        try:
            return await self._open_and_detect_live()
        finally:
            await self.release_idle_browser()'''
            assert s.count(old) == 1, f"[{tag}] 重试后命中 {s.count(old)} 次"
            s = s.replace(old, new, 1)
            continue
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)

# _after_probe 里"周期性完整检测"改走 fast
old_cycle = '''                logger.info(f"[{self.account['id']}] 轻量探测连续 {full_every} 次未播"
                            f"(status={probe.get('status')}),做一次完整检测(含页面文本)")
                return await self._open_full()'''
new_cycle = '''                logger.info(f"[{self.account['id']}] 轻量探测连续 {full_every} 次未播"
                            f"(status={probe.get('status')}),做一次快速完整检测(含页面文本)")
                return await self._open_full(fast=True)'''
assert s.count(old_cycle) == 1, s.count(old_cycle)
s = s.replace(old_cycle, new_cycle, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

s = io.open(P, encoding="utf-8").read()
checks = [
    ("_open_and_detect_live 带 fast", "async def _open_and_detect_live(self, fast=False):" in s),
    ("等待超时按 fast 取值", "timeout=4.0 if fast else 20.0" in s),
    ("relearn 可关", "allow_relearn=not fast" in s),
    ("wait 支持 allow_relearn", "async def _wait_for_live_detection(self, lf, timeout=20.0, allow_relearn=True):" in s),
    ("_open_full 带 fast", "async def _open_full(self, fast=False):" in s),
    ("周期检测走 fast", "await self._open_full(fast=True)" in s),
]
bad = [n for n, o in checks if not o]
for n, o in checks:
    print(("  OK   " if o else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS fast confirm")
