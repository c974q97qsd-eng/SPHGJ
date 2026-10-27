"""rev49 单元测试: live_probe_full_every 的 0 语义 + 开播路径不受影响。

跑法: python tools/test_rev49_full_every.py
"""
import asyncio
import sys
import types

sys.path.insert(0, r"D:\SPHGJ")


def _stub_playwright():
    """本地开发机没装 playwright（只有母本装），用空壳顶上。

    被测的 _after_probe 是纯控制流，不碰任何浏览器对象，所以 stub 是安全的。
    """
    class _Any:
        def __getattr__(self, k):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

        def __await__(self):
            yield
            return _Any()

    for name in ("playwright", "playwright.async_api", "playwright.sync_api",
                 "playwright._impl", "playwright._impl._errors"):
        m = types.ModuleType(name)
        m.__path__ = []
        m.__getattr__ = lambda k: _Any()
        sys.modules[name] = m


_stub_playwright()

from backend.account_manager import AccountWorker   # noqa: E402

PROBE_DOWN = {"live": False, "reason": "status0", "status": 0, "via": "http"}
PROBE_LIVE = {"live": True, "reason": "stats", "status": 1, "via": "http"}


def make_worker(full_every):
    w = object.__new__(AccountWorker)
    w.config = {"live_probe_full_every": full_every}
    w.account = {"id": "test"}
    w._probe_since_full = 0
    w._last_probe = None
    w.calls = []

    async def fake_open_full(fast=False):
        w.calls.append(("open_full", fast))
        return True

    w._open_full = fake_open_full
    return w


async def main():
    # 1) full_every=0 -> 20 次未播，一次浏览器都不开
    w = make_worker(0)
    for _ in range(20):
        assert await w._after_probe(PROBE_DOWN) is False
    assert w.calls == [], f"0 应完全禁用完整检测，实际 {w.calls}"
    print("PASS  full_every=0 -> 20 次未播，0 次完整检测")

    # 2) 在播时仍然开大屏页（功能未减）
    w = make_worker(0)
    assert await w._after_probe(PROBE_LIVE) is True
    assert w.calls == [("open_full", False)], w.calls
    print("PASS  full_every=0 但在播 -> 立刻 _open_full(fast=False)")

    # 3) 轻量探测不可用(None) -> 退化到完整检测，保底路径还在
    w = make_worker(0)
    assert await w._after_probe(None) is True
    assert w.calls == [("open_full", False)], w.calls
    print("PASS  probe=None -> 退化 _open_full（保底路径保留）")

    # 4) full_every>0 时恢复原行为：每 N 次未播落一次 fast 检测
    w = make_worker(3)
    for _ in range(9):
        await w._after_probe(PROBE_DOWN)
    assert len(w.calls) == 3, f"9 次未播应触发 3 次，实际 {len(w.calls)}"
    assert all(c == ("open_full", True) for c in w.calls), w.calls
    print("PASS  full_every=3 -> 9 次未播触发 3 次 fast 完整检测")

    # 5) full_every 缺省(配置里没写) -> 默认 0
    w = make_worker(0)
    w.config = {}
    for _ in range(20):
        await w._after_probe(PROBE_DOWN)
    assert w.calls == [], f"缺省应等于 0，实际 {w.calls}"
    print("PASS  配置缺省 -> 默认 0")

    print("\nALL PASS (5/5)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
