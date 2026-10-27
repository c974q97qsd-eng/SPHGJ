# -*- coding: utf-8 -*-
"""rev47 patch 2/2: 直播探测"频率解耦 + 状态机合并"

用户 2026-10-28 选的方向。改 account_manager.py:

A. 新增三级轻量探测(不开直播大屏页):
     L0 已有 context -> ctx.request 发一次 check_live_status(约 0.3s)
     L1 没有 context -> 只建 context(不导航评论页)探完即关(约 3~5s)
     L2 拿不到规格/请求失败 -> 退回原来的完整检测(开大屏页 + DOM 文本)
   实测依据:完整检测一轮要 141 秒(01:47:12→01:49:33),而它做的事其实就是问一次
   check_live_status。把"问"和"开大屏页"拆开,是提频的前提。

B. 三个入口合并:
     原 _fetch_loop._cycle_live_check(跟 600s 抓取、夜间 ×3)
     原 _live_probe_loop(仅手动启动的 180s 窗口、每 24s)
     原 _live_loop(在播时)
   -> 统一到 _live_detect_loop + _probe_and_maybe_open(),共用 _last_probe_ts 节流。
   夜间不再降频:直播集中在夜里,原来的 ×3 让开播最坏 30 分钟才发现。

C. 判定统一:检测 / 等待 / 直播循环三处全部改用 live_fetcher.judge_live()。
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = []

# ---------------------------------------------------------------- 1. import
PATCHES.append((
    'from .live_fetcher import LiveFetcher',
    'from .live_fetcher import LiveFetcher, judge_live, _load_spec_cache',
    "import",
))

# ---------------------------------------------------------------- 2. 状态字段
PATCHES.append((
    '''        # rev16 F2: 判定信号丢失后原地重试 poll 的计数(信号恢复即清零)
        self._live_poll_retry = 0''',
    '''        # rev16 F2: 判定信号丢失后原地重试 poll 的计数(信号恢复即清零)
        self._live_poll_retry = 0
        # ======================== rev47: 统一直播探测器 ========================
        # 原实现三个入口各管一段:_fetch_loop 里的 _cycle_live_check(跟 600s 抓取周期,
        # 夜间 ×3 = 30 分钟)、_live_probe_loop(仅手动启动的 180s 窗口)、_live_loop(在播时)。
        # 后果:① 开播最坏要等 30 分钟才发现;② 同一账号 90 秒内被检测两次
        # (实测 4 号 01:47:12 与 01:48:58);③ 每次检测开一整个直播大屏 SPA,141 秒才收敛。
        # rev47 起只留一个探测器:轻量探测(一次 HTTP)为主,周期性才落完整检测。
        self._detect_task = None
        self._probe_wake = asyncio.Event()
        self._probe_boost_until = 0.0   # 手动启动后的高频探测窗口(原 _probe_window 语义)
        self._last_probe_ts = 0.0       # 最近一次探测时间(探测器与抓取周期共用节流)
        self._last_probe = {}           # 最近一次探测结果(诊断用)
        self._probe_since_full = 0      # 距上次"完整检测"的轻量探测次数''',
    "state",
))

# ---------------------------------------------------------------- 3. 轻量探测三件套
PATCHES.append((
    '''    async def _open_and_detect_live(self):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。''',
    '''    # ======================== rev47: 轻量直播探测(不开大屏页) ========================

    async def _open_context_light(self):
        """只建 context(注入登录态),不导航评论页 —— 专供轻量直播探测。

        与 _open_context 的区别:不 goto COMMENT_URL、不 wait 2.5s、不建 WxApiClient、
        不做登录态硬验(那是评论抓取要用的)。探测只需 cookie 在场,一次
        check_live_status 就够,省掉 3~5 秒页面加载和一个 renderer。
        调用方必须持有 _browser_lock,用完自己 close(绝不写回 self.context)。
        """
        ctx = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        try:
            _ck = ((self.account.get("storage_state") or {}).get("cookies") or [])
            if _ck:
                await ctx.add_cookies(_ck)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测注入登录态失败: {e}")
        return ctx

    async def _probe_via_context(self, ctx):
        """用一次 check_live_status 请求探测直播状态。返回判据 dict 或 None。

        走 APIRequestContext(ctx.request)而不是 page.evaluate:它共享同一个 cookie jar,
        但完全不经过 renderer —— 没有页面加载、没有 SPA 水合、没有网络事件进 Python。
        这是"把探测频率提到分钟级"的前提。

        解析仍复用 LiveFetcher._parse_live_status(构造一个 page=None 的临时实例),
        保证"轻量探测"和"大屏页探测"对同一份响应得出同样的结论 —— 不要再写第二份解析。
        """
        spec = (_load_spec_cache(self.account["id"]) or {}).get("live_status")
        if not spec or not spec.get("url"):
            return None
        headers = {k: v for k, v in (spec.get("headers") or {}).items()
                   if k.lower() not in ("cookie", "content-length", "host")}
        body = spec.get("postData")
        url = spec["url"]
        try:
            if (spec.get("method") or "POST").upper() == "GET":
                r = await ctx.request.get(url, headers=headers, timeout=15000)
            else:
                r = await ctx.request.post(url, headers=headers, data=body or None, timeout=15000)
            code = r.status
            txt = await r.text()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测请求失败: {e}")
            return None
        if not txt or code >= 400:
            logger.debug(f"[{self.account['id']}] 轻量探测响应异常(status={code}, len={len(txt or '')})")
            return None
        lf = None
        try:
            lf = LiveFetcher(None, self.account["id"])
            # 抑制"拿到 liveObjectId, dashboard 抓取启用"这类只对常驻实例有意义的日志
            # 与首次落盘(轻量探测每几分钟一次,不该重复写文件)
            lf.live_object_id = "-"
            lf._status_body_dumped = True
            await lf._parse_live_status(txt)
            live, reason = judge_live(
                live_stats=lf.live_stats, stats_fresh=True,
                stream_url=lf.stream_url, allow_stream_url=True,
                status=lf.status_num)
            return {"ts": time.time(), "live": live, "reason": reason,
                    "status": lf.status_num, "has_stream": bool(lf.stream_url),
                    "has_stats": bool(lf.live_stats), "via": "light"}
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测解析失败: {e}")
            return None
        finally:
            lf = None
            txt = None

    async def _probe_light(self):
        """轻量探测:一次 HTTP,不开直播大屏页。返回判据 dict 或 None(不可用)。

        L0 已有 context(正在抓评论/手动操作)-> 直接 ctx.request,约 0.3s,零新增开销;
        L1 没有 context -> 只建 context 探完即关,约 3~5s(远小于开大屏页的 30~140s);
        两条路都失败 -> 返回 None,调用方退回完整检测。
        """
        try:
            async with self._browser_lock:
                if self.context is not None:
                    return await self._probe_via_context(self.context)
                try:
                    ctx = await self._open_context_light()
                except Exception as e:
                    logger.debug(f"[{self.account['id']}] 轻量探测开浏览器失败: {e}")
                    return None
                try:
                    return await self._probe_via_context(ctx)
                finally:
                    try:
                        await close_context_safely(ctx, self.account.get("profile_dir"),
                                                   f"[{self.account['id']}] 轻量探测收回")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测异常: {e}")
            return None

    async def _probe_and_maybe_open(self):
        """rev47: 唯一探测入口 —— 轻量判定,命中或周期性才开直播大屏页。返回 is_live。

        探测器循环 / 评论抓取周期 / 手动启动窗口都走这里,共用 _last_probe_ts 节流。
        """
        if self.live_fetcher is not None:
            return True
        if not self.logged_in:
            return False
        self._last_probe_ts = time.time()
        probe = await self._probe_light()
        if probe is not None:
            self._last_probe = probe
            self._probe_since_full += 1
            if probe["live"]:
                logger.info(f"[{self.account['id']}] 轻量探测:在播(依据={probe['reason']}),"
                            f"打开直播大屏页取流")
                return await self._open_and_detect_live()
            # 未播:轻量探测不开大屏页,直接收工。但每 N 次(默认 5)落一次完整检测,
            # 让 DOM 文本这条"接口不认、页面认"的兜底判据仍有出场机会。
            full_every = max(1, int(self.config.get("live_probe_full_every", 5)))
            if self._probe_since_full >= full_every:
                self._probe_since_full = 0
                logger.info(f"[{self.account['id']}] 轻量探测连续 {full_every} 次未播"
                            f"(status={probe.get('status')}),做一次完整检测(含页面文本)")
                return await self._open_and_detect_live()
            logger.debug(f"[{self.account['id']}] 轻量探测:未播(依据={probe['reason']},"
                         f"status={probe.get('status')})")
            return False
        return await self._open_and_detect_live()

    async def _open_and_detect_live(self):
        """开 live_page 并导航,等待 check_live_status/get_live_info 响应后判定是否直播。''',
    "light",
))

# ---------------------------------------------------------------- 4. goto 后读 DOM + 统一判据
PATCHES.append((
    '''            await lf.goto_live()
            self._live_opened_ts = time.time()
            self._live_nosignal_reloads = 0''',
    '''            await lf.goto_live()
            self._live_opened_ts = time.time()
            self._live_nosignal_reloads = 0
            # rev47: 接口层没有"在播"布尔位(未开播时只有 status=0),所以页面文本
            # 是最廉价、最不依赖接口字段的兜底判据。只读文本,不点任何按钮。
            try:
                await lf.read_dom_state(dump=True)
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 读直播页文本失败: {e}")''',
    "goto-dom",
))

PATCHES.append((
    '''            last_ts = lf.updated_at_ts or 0
            fresh = (time.time() - last_ts) < 30
            is_live = bool(info.get("live_stats") and fresh)
            if not is_live and lf.stream_url:
                is_live = True
                logger.info(f"[{self.account['id']}] 检测到直播信号(stream_url 已取到),开始流式推送")
            if is_live:''',
    '''            now_ts = time.time()
            last_ts = lf.updated_at_ts or 0
            # rev47: 统一判据(见 live_fetcher.judge_live)。检测窗口只有 30 秒,
            # 不存在"上一场残留的 sticky 流地址",所以这里允许用 stream_url。
            is_live, why = judge_live(
                live_stats=info.get("live_stats"), stats_fresh=(now_ts - last_ts) < 30,
                stream_url=lf.stream_url, allow_stream_url=True,
                dom_state=info.get("dom_state"),
                dom_fresh=(now_ts - (info.get("dom_ts") or 0)) < 120,
                status=info.get("status_num"))
            if is_live:
                logger.info(f"[{self.account['id']}] 检测到直播信号(依据={why}),开始流式推送")''',
    "judge-detect",
))

PATCHES.append((
    '''            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(live_stats={info.get('live_stats') is not None}, "
                        f"live_object_id={lf.live_object_id}, "
                        f"learned={sorted(getattr(lf, '_learned', {}) or {})}, "
                        f"cdp_fallback={getattr(lf, '_cdp_fallback', None)}, "
                        f"url={(info.get('page_url') or '')[-40:]})")''',
    '''            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(依据={why}, live_stats={info.get('live_stats') is not None}, "
                        f"status={info.get('status_num')}, dom={info.get('dom_state')}, "
                        f"live_object_id={lf.live_object_id}, "
                        f"learned={sorted(getattr(lf, '_learned', {}) or {})}, "
                        f"cdp_fallback={getattr(lf, '_cdp_fallback', None)}, "
                        f"url={(info.get('page_url') or '')[-40:]})")''',
    "judge-log",
))

# ---------------------------------------------------------------- 5. 等待循环同判据
PATCHES.append((
    '''        def _hit(i):
            return bool(i.get("live_stats") or lf.stream_url)''',
    '''        def _hit(i):
            # rev47: 与 _open_and_detect_live 共用同一个判据函数。
            # rev44 那次事故就是这里与判定各写一套(拿 live_object_id 就提前返回),
            # 以后不许再就地写表达式。
            ok, _ = judge_live(
                live_stats=i.get("live_stats"), stats_fresh=True,
                stream_url=lf.stream_url, allow_stream_url=True,
                dom_state=i.get("dom_state"),
                dom_fresh=(time.time() - (i.get("dom_ts") or 0)) < 120,
                status=i.get("status_num"))
            return ok''',
    "hit",
))

# ---------------------------------------------------------------- 6. _cycle_live_check 走统一探测
PATCHES.append((
    '''        if self.live_fetcher is not None:
            return
        await self._open_and_detect_live()

    def start_loop(self):''',
    '''        if self.live_fetcher is not None:
            return
        # rev47: 抓取结束时 context 还开着(L0),这次探测几乎零成本 —— 只是多一个
        # check_live_status 请求,不再为此另开一个直播大屏页。
        await self._probe_and_maybe_open()

    def start_loop(self):''',
    "cycle",
))

# ---------------------------------------------------------------- 7. 探测器替换 _live_probe_loop
PATCHES.append((
    '''    def start_live_probe(self):
        """手动启动后短期开播探测:高频查直播,尽快捕获"启动后开播"。

        仅手动 start_account 调用(用户主动操作,短期开进程可接受)。复用 _cycle_live_check
        (已有的直播检测逻辑),窗口内每 ~24s 查一次,检测到直播则转 _live_loop 流式;
        窗口结束仍未播则收回进程,交回 10 分钟抓取周期兜底。
        """
        if self._live_probe_task is None or self._live_probe_task.done():
            self._live_probe_task = asyncio.create_task(self._live_probe_loop())

    async def _live_probe_loop(self):
        probe_until = time.time() + self._probe_window
        interval = self.config.get("live_check_interval_sec", 8)
        ok = await self.ensure_browser()
        if not ok:
            return
        try:
            while self._running and time.time() < probe_until:
                if self.logged_in:
                    # 借用全局抓取锁,避免与 10 分钟抓取并发拉起浏览器
                    if self._fetch_lock is not None:
                        await self._fetch_lock.acquire()
                    try:
                        await self._cycle_live_check()
                    finally:
                        if self._fetch_lock is not None:
                            try: self._fetch_lock.release()
                            except Exception: pass
                    if self.live_fetcher is not None:
                        logger.info(f"[{self.account['id']}] 开播探测窗口内检测到直播,转流式推送")
                        return
                await asyncio.sleep(interval * 3)
        finally:
            # 未播:收回进程(live_fetcher 为 None 时 release 会关 context);
            # 已播:live_fetcher 非 None,release 提前返回不关,保持流式。
            await self.release_idle_browser()''',
    '''    def start_live_probe(self):
        """手动启动后进入高频探测窗口,尽快捕获"启动后开播"。

        rev47: 不再单独起一个 _live_probe_loop(原来要自己借 _fetch_lock、每 24s
        重新开一次浏览器)。改为给统一探测器打一个 boost 标记并唤醒它 —— 窗口内
        间隔降到 live_probe_boost_interval_sec(默认 20s),窗口结束自动回落。
        """
        self._probe_boost_until = time.time() + self._probe_window
        try:
            self._probe_wake.set()
        except Exception:
            pass
        logger.info(f"[{self.account['id']}] 开播探测窗口 {int(self._probe_window)}s 内高频探测"
                    f"(间隔 {int(self.config.get('live_probe_boost_interval_sec', 20))}s)")

    def start_live_detect(self):
        """rev47: 启动统一探测器(每个账号一个,常驻但绝大多数时间在睡)。"""
        if self._detect_task is None or self._detect_task.done():
            self._detect_task = asyncio.create_task(self._live_detect_loop())

    async def _live_detect_loop(self):
        """rev47: 全账号唯一的直播探测节拍器。

        频率解耦:评论抓取仍是 fetch_interval_sec(600s,夜间 ×3),直播探测走独立的
        live_probe_interval_sec(默认 300s),【不再乘夜间倍率】—— 直播本来就集中在
        夜里,原来的 ×3 让开播最坏 30 分钟才发现。
        在播期间本循环只是睡(交给 _live_loop),不参与任何探测。
        """
        interval = max(30, int(self.config.get("live_probe_interval_sec", 300)))
        boost_interval = max(5, int(self.config.get("live_probe_boost_interval_sec", 20)))
        gap = float(self.config.get("live_probe_min_gap_sec", 60))
        await asyncio.sleep(random.uniform(0, 20))   # 多账号错峰,避免同时拉起浏览器
        while self._running:
            wait = interval
            try:
                self._probe_wake.clear()
                if self.live_fetcher is None and self.logged_in:
                    boost = time.time() < self._probe_boost_until
                    wait = boost_interval if boost else interval
                    # 节流:抓取周期可能刚顺带探过,别立刻再探一次
                    if time.time() - (self._last_probe_ts or 0) >= gap:
                        await self._probe_and_maybe_open()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[{self.account['id']}] 直播探测循环异常: {e}")
                wait = 60
            try:
                # 用 Event 等,start_live_probe / 手动检测可以立刻唤醒,不必等满 interval
                await asyncio.wait_for(self._probe_wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise''',
    "detect-loop",
))

# ---------------------------------------------------------------- 8. start() 起探测器
PATCHES.append((
    '''        if self.logged_in:
            await self._initial_live_check()''',
    '''        if self.logged_in:
            await self._initial_live_check()
        # rev47: 常驻探测器(统一节拍,取代原先"跟抓取周期 + 手动窗口"两套)
        self.start_live_detect()''',
    "start",
))

# ---------------------------------------------------------------- 9. stop() 取消探测器
PATCHES.append((
    '''        if self._live_probe_task:
            self._live_probe_task.cancel()''',
    '''        if self._detect_task:
            self._detect_task.cancel()
            try:
                await self._detect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._detect_task = None
        # rev47: _live_probe_task 已被统一探测器取代,保留取消逻辑处理旧引用(升级/热重启)
        if self._live_probe_task:
            self._live_probe_task.cancel()''',
    "stop",
))

# ---------------------------------------------------------------- 10. _live_loop 判据统一
PATCHES.append((
    '''                    last_ts = lf.updated_at_ts or 0
                    signal_lost_sec = int(self.config.get("live_signal_lost_sec", 90))
                    info["is_live"] = bool(info.get("live_stats") and (now - last_ts < signal_lost_sec))''',
    '''                    last_ts = lf.updated_at_ts or 0
                    signal_lost_sec = int(self.config.get("live_signal_lost_sec", 90))
                    # rev47: 没有 live_stats 时补一次页面文本采样(最多每 30s 一次),
                    # 这是"接口不认、页面认"的兜底判据 —— 长驻页面最容易遇到接口静默。
                    if not info.get("live_stats") and now - (getattr(lf, "dom_ts", 0) or 0) > 30:
                        try:
                            await lf.read_dom_state(dump=False)
                            info["dom_state"] = lf.dom_state
                            info["dom_ts"] = lf.dom_ts
                        except Exception:
                            pass
                    # rev47: 统一判据。长驻页面【禁止】用 stream_url 判活 —— 它是 sticky 的,
                    # 拿到一次就再也不清空,用它判活会永远判"在播"、永远不回收页面。
                    _sig, _why = judge_live(
                        live_stats=info.get("live_stats"),
                        stats_fresh=(now - last_ts) < signal_lost_sec,
                        allow_stream_url=False,
                        dom_state=info.get("dom_state"),
                        dom_fresh=(now - (info.get("dom_ts") or 0)) < 90,
                        status=info.get("status_num"))
                    info["is_live"] = _sig
                    info["live_reason"] = _why''',
    "livesig",
))

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------------------------------------------------------- 自校验
s = io.open(P, encoding="utf-8").read()
checks = [
    ("import judge_live", "judge_live, _load_spec_cache" in s),
    ("探测器字段", "self._detect_task = None" in s and "self._probe_wake" in s),
    ("_open_context_light", "async def _open_context_light(self)" in s),
    ("_probe_via_context", "async def _probe_via_context(self, ctx)" in s),
    ("_probe_light", "async def _probe_light(self)" in s),
    ("_probe_and_maybe_open", "async def _probe_and_maybe_open(self)" in s),
    ("_live_detect_loop", "async def _live_detect_loop(self)" in s),
    ("start_live_detect", "def start_live_detect(self)" in s),
    ("旧的 _live_probe_loop 已删", "async def _live_probe_loop" not in s),
    ("检测用统一判据", "依据={why}" in s),
    ("等待循环用统一判据", "与 _open_and_detect_live 共用同一个判据函数" in s),
    ("直播循环禁 stream_url", "allow_stream_url=False" in s),
    ("抓取周期走轻量探测", "await self._probe_and_maybe_open()" in s),
    ("start 起探测器", "self.start_live_detect()" in s),
    ("stop 取消探测器", "if self._detect_task:" in s),
    ("没有就地判 is_live 的旧表达式", 'is_live = bool(info.get("live_stats") and fresh)' not in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK   " if ok else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS  account_manager.py")
