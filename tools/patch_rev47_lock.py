# -*- coding: utf-8 -*-
"""rev47 patch 3: 探测互斥锁 —— 同一账号同一时刻只允许一次探测。

为什么需要:rev47 之后探测有三个触发源(常驻探测器 / 评论抓取周期 / 手动启动窗口),
它们各自判断 self.live_fetcher is None 后就去探测。若两路同时通过这个检查,会出现
两个探测并发:轻量探测这边拿着 context 发请求,那边 _open_and_detect_live 又在同一个
context 上新开 live_page —— 后者不经过 _browser_lock,两边可能互相踩。
加一把 _probe_lock 把"检查 + 探测"整体串行,锁内再确认一次,后到者直接返回。
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = [
    (
        '''        self._probe_since_full = 0      # 距上次"完整检测"的轻量探测次数''',
        '''        self._probe_since_full = 0      # 距上次"完整检测"的轻量探测次数
        # 探测互斥:三个触发源(常驻探测器/抓取周期/手动窗口)必须先抢这把锁,
        # 锁内再确认一次 live_fetcher,避免并发探测(一个持 context 探测、另一个
        # 在同一 context 上新开 live_page,而后者不经过 _browser_lock)。
        self._probe_lock = asyncio.Lock()''',
        "probe-lock-field",
    ),
    (
        '''        if self.live_fetcher is not None:
            return True
        if not self.logged_in:
            return False
        self._last_probe_ts = time.time()
        probe = await self._probe_light()''',
        '''        if self.live_fetcher is not None:
            return True
        if not self.logged_in:
            return False
        async with self._probe_lock:
            # 锁内二次确认:等锁期间可能已经有人探测完并把 live_page 开起来了
            if self.live_fetcher is not None:
                return True
            self._last_probe_ts = time.time()
            probe = await self._probe_light()
            return await self._after_probe(probe)

    async def _after_probe(self, probe):
        """轻量探测结果 -> 是否开直播大屏页。probe=None 表示轻量不可用,退回完整检测。"""''',
        "probe-lock-use",
    ),
    (
        '''        if probe is not None:
            self._last_probe = probe
            self._probe_since_full += 1''',
        '''        if probe is not None:
            self._last_probe = probe
            self._probe_since_full += 1''',
        "noop-anchor",
    ),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    if tag == "noop-anchor":
        assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次"
        continue
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)

# _after_probe 内部原本用的是 _probe_and_maybe_open 的局部变量,现在要能独立成立:
# 把 "return await self._open_and_detect_live()" 之前那段保持原样即可(probe 已是参数)。
io.open(P, "w", encoding="utf-8", newline="").write(s)

s = io.open(P, encoding="utf-8").read()
checks = [
    ("_probe_lock 字段", "self._probe_lock = asyncio.Lock()" in s),
    ("_probe_lock 使用", "async with self._probe_lock:" in s),
    ("_after_probe 定义", "async def _after_probe(self, probe):" in s),
    ("_after_probe 调用", "return await self._after_probe(probe)" in s),
    ("_probe_and_maybe_open 仍存在", "async def _probe_and_maybe_open(self)" in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK   " if ok else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS probe lock")
