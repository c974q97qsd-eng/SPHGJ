# -*- coding: utf-8 -*-
"""rev52 step5: 修引用计数配对。

_run_fetch_round 走纯 HTTP 时没有 ensure_browser(+1),但 _fetch_loop 的 finally 里
无条件 release_idle_browser(-1) —— 会把别的调用方(手动回复 hold_for_manual)的引用
吃掉,导致手动操作期间浏览器被提前关掉。

改法:_run_fetch_round 一并返回「本轮是否用了浏览器」,只在用过时 release。
"""
import io
import sys

P = r"D:\SPHGJ\backend\account_manager.py"
s = io.open(P, encoding="utf-8").read()
orig = s
PATCHES = []

# 1) _fetch_loop:局部标记 + 条件释放
PATCHES.append((
    '''            if self._fetch_lock is not None:
                await self._fetch_lock.acquire()
            try:
                res = await self._run_fetch_round()
                if res is None:
                    await asyncio.sleep(min(interval, 60))
                    continue''',
    '''            if self._fetch_lock is not None:
                await self._fetch_lock.acquire()
            used_browser = False
            try:
                res, used_browser = await self._run_fetch_round()
                if res is None:
                    await asyncio.sleep(min(interval, 60))
                    continue''',
    "_fetch_loop 局部标记",
))

PATCHES.append((
    '''            finally:
                if self._fetch_lock is not None:
                    try: self._fetch_lock.release()
                    except Exception: pass
                # 无动作(非直播/无手动操作)则收回浏览器进程省内存
                await self.release_idle_browser()''',
    '''            finally:
                if self._fetch_lock is not None:
                    try: self._fetch_lock.release()
                    except Exception: pass
                # 无动作(非直播/无手动操作)则收回浏览器进程省内存。
                # rev52: 只有本轮真的开过浏览器才释放 —— 纯 HTTP 那一轮没有对应的
                # ensure_browser(+1),无条件 release 会吃掉手动操作/直播持有的引用计数。
                if used_browser:
                    await self.release_idle_browser()''',
    "_fetch_loop 条件释放",
))

# 2) _run_fetch_round 返回 (res, used_browser)
PATCHES.append((
    '''        返回 5 元组;连登录态都拿不到时返回 None(调用方等一小会儿再来一轮)。
        """
        if self.fetcher is None:
            return None''',
    '''        返回 (5 元组, 是否用了浏览器);连登录态都拿不到时返回 (None, False)。
        """
        if self.fetcher is None:
            return None, False''',
    "签名注释",
))

PATCHES.append((
    '''                if res[4]:
                    if self.context is None:
                        logger.debug(f"[{self.account['id']}] 本轮抓取全程 HTTP 直连(未开浏览器)")
                    return res''',
    '''                if res[4]:
                    if self.context is None:
                        logger.debug(f"[{self.account['id']}] 本轮抓取全程 HTTP 直连(未开浏览器)")
                    return res, False''',
    "HTTP 成功返回",
))

PATCHES.append((
    '''        ok = await self.ensure_browser()
        if not ok:
            logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
            return None
        if not self.logged_in:
            return None
        try:
            return await self.fetcher.fetch_all(on_batch=self._emit_comment_batch)
        except Exception as e:
            logger.error(f"[{self.account['id']}] 抓取异常: {e}")
            return None''',
    '''        ok = await self.ensure_browser()
        if not ok:
            logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
            return None, False
        if not self.logged_in:
            return None, True
        try:
            return await self.fetcher.fetch_all(on_batch=self._emit_comment_batch), True
        except Exception as e:
            logger.error(f"[{self.account['id']}] 抓取异常: {e}")
            return None, True''',
    "浏览器路径返回",
))

# 3) fetch_once 适配二元组
PATCHES.append((
    '''        res = await self._run_fetch_round()
        await self.release_idle_browser()
        return res if res is not None else (0, 0, [], [], False)''',
    '''        res, used_browser = await self._run_fetch_round()
        if used_browser:
            await self.release_idle_browser()
        return res if res is not None else (0, 0, [], [], False)''',
    "fetch_once 适配",
))

for old, new, tag in PATCHES:
    if s.count(old) != 1:
        print("FAIL 命中 %d 次: %s" % (s.count(old), tag))
        sys.exit(1)
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

for ok, tag in [
    ("res, used_browser = await self._run_fetch_round()" in s, "解包二元组"),
    ("if used_browser:\n                    await self.release_idle_browser()" in s, "条件释放"),
    ("return None, False" in s, "None,False"),
    ("return res, False" in s, "HTTP 路径 False"),
    ("self.fetcher.fetch_all(on_batch=self._emit_comment_batch), True" in s, "浏览器路径 True"),
]:
    print(("PASS " if ok else "FAIL ") + tag)
print("bytes %d -> %d" % (len(orig.encode()), len(s.encode())))
