# -*- coding: utf-8 -*-
"""rev47 patch 4: 完整检测前补齐浏览器引用。

缺口:改由探测器调用后,_after_probe 可能在 self.context 为 None 时落到
      _open_and_detect_live(),而它内部直接用 self.context.new_page() -> AttributeError
      -> 日志只留一条"直播检测失败",探测等于白跑。
     原实现没暴露这个问题,是因为 _cycle_live_check 一直在 _fetch_loop 里被调用
     (_fetch_loop 已 ensure_browser 过),而探测器是独立节拍,没有这个前提。

修法:新增 _open_full(),context 不存在就先 ensure_browser 借一份引用,用完 finally 释放。
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = [
    (
        '''    async def _after_probe(self, probe):
        """轻量探测结果 -> 是否开直播大屏页。probe=None 表示轻量不可用,退回完整检测。"""''',
        '''    async def _open_full(self):
        """完整检测(开直播大屏页)。探测器可能在没有浏览器时调用,这里负责补引用。"""
        if self.context is not None:
            return await self._open_and_detect_live()
        if not await self.ensure_browser():
            logger.debug(f"[{self.account['id']}] 完整检测跳过:开浏览器失败")
            return False
        try:
            return await self._open_and_detect_live()
        finally:
            await self.release_idle_browser()

    async def _after_probe(self, probe):
        """轻量探测结果 -> 是否开直播大屏页。probe=None 表示轻量不可用,退回完整检测。"""''',
        "open-full-def",
    ),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)

# _after_probe 内部的 3 处 _open_and_detect_live() 改为走 _open_full()
start = s.index("    async def _after_probe(self, probe):")
end = s.index("    async def _open_and_detect_live(self):")
body = s[start:end]
n = body.count("return await self._open_and_detect_live()")
assert n == 3, f"_after_probe 内应命中 3 处,实际 {n} 处"
body = body.replace("return await self._open_and_detect_live()", "return await self._open_full()")
s = s[:start] + body + s[end:]
io.open(P, "w", encoding="utf-8", newline="").write(s)

s = io.open(P, encoding="utf-8").read()
checks = [
    ("_open_full 定义", "async def _open_full(self):" in s),
    ("_after_probe 走 _open_full", s.count("return await self._open_full()") == 3),
    ("不再直接调 _open_and_detect_live(除 _initial/_cycle)",
     s.count("await self._open_and_detect_live()") == 2),
    ("_open_full 里 borrow 释放", "await self.release_idle_browser()" in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK   " if ok else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS open_full")
