# -*- coding: utf-8 -*-
"""rev52 step3: account_manager
  1) __init__ 建一个「无浏览器」的 WxApiClient,让抓取可以完全不启动 Chromium
  2) _fetch_loop 抽出 _run_fetch_round():HTTP 直连优先,走不通才开浏览器
  3) fetch_all 返回值变 5 元组(多一个"是否完整")
  4) 直播探测未播间隔 120 -> 300(开播仍走 boost 30s)
"""
import io
import sys

P = r"D:\SPHGJ\backend\account_manager.py"
s = io.open(P, encoding="utf-8").read()
orig = s
PATCHES = []

# ---------- 1) __init__ 建无浏览器 api,并绑给所有组件 ----------
PATCHES.append((
    '''        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete,
                                      finder_getter=_fid)''',
    '''        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete,
                                      finder_getter=_fid, config=self.config)
        # rev52: 先建一个「无浏览器」的 API 客户端。rev50 之后读接口全程走进程内 HTTP,
        # 浏览器只在 HTTP 走不通时才需要 —— 但原来这条路径必须先 ensure_browser()
        # 才能拿到 api/fetcher,等于每轮强制 launch Chromium(8~15s)+ goto + auth 硬验,
        # 8 个账号每天 120 轮 = 960 次启停。这里预建一个 page=None 的客户端,
        # _open_context() 之后仍会重建带 page 的版本覆盖(浏览器 = HTTP 的回退路径)。
        self.api = WxApiClient(None, self.account, self.config, self.storage)
        self.auto_reply.api = self.api
        self.auto_commenter.api = self.api
        self.auto_delete.api = self.api
        self.fetcher.api = self.api''',
    "__init__ 预建 HTTP 客户端",
))

# ---------- 2) _fetch_loop 的抓取块 ----------
OLD_FETCH = '''            try:
                ok = await self.ensure_browser()
                if not ok:
                    logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
                    await asyncio.sleep(min(interval, 60))
                    continue
                if self.logged_in and self.fetcher:
                    try:
                        # rev15 P2: 传 on_batch 启用流式推送,避免高评论量账号在内存堆积全量新评论
                        scanned, new_c, new_comments, deleted_ids = await self.fetcher.fetch_all(
                            on_batch=self._emit_comment_batch)
                        self.last_scan_at = datetime.now().isoformat()
                        if new_c:
                            today = datetime.now().strftime("%Y-%m-%d")
                            if self._new_count_date != today:
                                self._new_count_date = today
                                self.new_count = 0
                            self.new_count += new_c
                        logger.info(f"[{self.account['id']}] 扫描{scanned}视频 新增{new_c}评论")
                        if new_comments:
                            await self._emit("comments_update",
                                             {"account_id": self.account["id"], "comments": new_comments})
                        for cid in deleted_ids:
                            await self._emit("comment_deleted", {"comment_id": cid})
                    except Exception as e:
                        logger.error(f"[{self.account['id']}] 抓取异常: {e}")
                # 周期内顺带检测直播(非直播则收回进程)
                await self._cycle_live_check()'''

NEW_FETCH = '''            try:
                res = await self._run_fetch_round()
                if res is None:
                    await asyncio.sleep(min(interval, 60))
                    continue
                scanned, new_c, new_comments, deleted_ids, complete = res
                self.last_scan_at = datetime.now().isoformat()
                if new_c:
                    today = datetime.now().strftime("%Y-%m-%d")
                    if self._new_count_date != today:
                        self._new_count_date = today
                        self.new_count = 0
                    self.new_count += new_c
                if not complete:
                    # rev52: 原来这种情况静默中断 —— 打的是「扫描N视频 新增0评论」INFO,
                    # 看起来这轮成功了,其实后面的作品压根没检查。
                    logger.warning(f"[{self.account['id']}] 本轮抓取不完整:有请求连续失败,"
                                   f"部分作品未检查(未覆盖部分下一轮会重新扫)")
                if new_comments:
                    await self._emit("comments_update",
                                     {"account_id": self.account["id"], "comments": new_comments})
                for cid in deleted_ids:
                    await self._emit("comment_deleted", {"comment_id": cid})
                # 周期内顺带检测直播(非直播则收回进程)
                await self._cycle_live_check()'''

PATCHES.append((OLD_FETCH, NEW_FETCH, "_fetch_loop 抓取块"))

# ---------- 3) 新增 _run_fetch_round ----------
ANCHOR = "    async def fetch_once(self):"
NEW_METHOD = '''    async def _run_fetch_round(self):
        """一轮评论抓取:HTTP 直连优先,只有走不通时才开浏览器。

        rev52: 原来每轮开头无条件 ensure_browser()。既然读接口(rev50)已经在进程内
        发 HTTP,浏览器就只在「HTTP 不可用」时才有价值 —— 典型是 6/9/10 号那种
        profile 磁盘上还登录着、但 config.storage_state 已过期(errCode=300333)的账号。

        返回 5 元组;连登录态都拿不到时返回 None(调用方等一小会儿再来一轮)。
        """
        if self.fetcher is None:
            return None
        # 1) 纯 HTTP:不需要任何 Chromium 进程
        http_ok = False
        if self.api is not None:
            try:
                http_ok = bool(self.api.http_available())
            except Exception:
                http_ok = False
        if http_ok:
            try:
                res = await self.fetcher.fetch_all(on_batch=self._emit_comment_batch)
                if res[4]:
                    if self.context is None:
                        logger.debug(f"[{self.account['id']}] 本轮抓取全程 HTTP 直连(未开浏览器)")
                    return res
                logger.info(f"[{self.account['id']}] HTTP 直连本轮不完整,开浏览器重跑(补漏)")
            except Exception as e:
                logger.warning(f"[{self.account['id']}] HTTP 抓取异常,改用浏览器: {e}")
        # 2) 浏览器回退
        ok = await self.ensure_browser()
        if not ok:
            logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
            return None
        if not self.logged_in:
            return None
        try:
            return await self.fetcher.fetch_all(on_batch=self._emit_comment_batch)
        except Exception as e:
            logger.error(f"[{self.account['id']}] 抓取异常: {e}")
            return None

'''
PATCHES.append((ANCHOR, NEW_METHOD + ANCHOR, "新增 _run_fetch_round"))

# ---------- 4) fetch_once 吃 5 元组 ----------
PATCHES.append((
    '''    async def fetch_once(self):
        ok = await self.ensure_browser()
        try:
            if ok and self.logged_in and self.fetcher:
                return await self.fetcher.fetch_all()
        finally:
            await self.release_idle_browser()
        return 0, 0, [], []''',
    '''    async def fetch_once(self):
        """手动「立即抓取一次」。同样先试 HTTP,失败再开浏览器。"""
        res = await self._run_fetch_round()
        await self.release_idle_browser()
        return res if res is not None else (0, 0, [], [], False)''',
    "fetch_once 适配",
))

# ---------- 5) 直播探测未播间隔 120 -> 300 ----------
PATCHES.append((
    '''        interval = max(30, int(self.config.get("live_probe_interval_sec", 120)))''',
    '''        # rev52: 未播间隔 120s -> 300s。轻量 HTTP 探测单次约 1.1s,8 个账号 120s 一轮
        # = 5760 次/天(占总请求 12%),而未播时探得再勤也只是"未播"。
        # 开播发现延迟由 180s 快速捕获窗口(start_live_probe 的 boost 30s)兜住,
        # 用户手动点检测也会立刻唤醒(Event),所以降频不影响"开播即被发现"。
        interval = max(30, int(self.config.get("live_probe_interval_sec", 300)))''',
    "探测间隔 300",
))

for old, new, tag in PATCHES:
    if s.count(old) != 1:
        print("FAIL 命中 %d 次: %s" % (s.count(old), tag))
        sys.exit(1)
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = [
    ("self.api = WxApiClient(None, self.account" in s, "预建无浏览器 api"),
    ("finder_getter=_fid, config=self.config)" in s, "fetcher 传 config"),
    ("async def _run_fetch_round" in s, "_run_fetch_round"),
    ("res = await self._run_fetch_round()" in s, "_fetch_loop 调用"),
    ("scanned, new_c, new_comments, deleted_ids, complete = res" in s, "5 元组解包"),
    ("live_probe_interval_sec\", 300" in s, "探测 300s"),
    ("self.api.http_available()" in s, "http_available 调用"),
]
for ok, tag in chk:
    print(("PASS " if ok else "FAIL ") + tag)
print("bytes %d -> %d" % (len(orig.encode()), len(s.encode())))
