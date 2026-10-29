# -*- coding: utf-8 -*-
"""rev60 补丁:启动被停机打断时,不许把账号误判成「未登录」并踢出恢复列表。

母本 01:19:46 同一秒的完整证据链:
    [重启] 收到重启指令,正在优雅退出…
    [shutdown] 开始清理(thread):账号进程 0 个      <- workers 是 0,因为还在恢复过程中
    [shutdown] 清理完成,用时 0.06s
    ERROR  [acc2] 启动开浏览器失败: Page.wait_for_timeout: Target page…
    WARNING [恢复] acc2 未登录或启动失败,已移出恢复列表

链条:
  ① restore_running_accounts 正在顺序启动 2 号(await w.start -> _open_context)
  ② stop_all 只看 self.workers(此时空),但照样 self._playwright.stop()
  ③ 正在进行的 goto/wait 被砍 -> 抛 Target page…closed
  ④ worker.start 把异常吞成一行 ERROR,logged_in 仍是 False、_verify_result 仍是 None
  ⑤ start_account 的 else 分支不看 _verify_result,直接 _remember_offline 落盘 offline
     -> data/auth_state.json 多一条「启动时 auth_data 硬验未通过」(现场实测已污染 2/6 号)
  ⑥ restore_running_accounts 见 aid 不在 workers -> discard 运行意图,下次重启不恢复

也就是「环境故障」被记成「登录失效」,同时丢掉用户「让它跑」的意图。
rev54/rev55 立的铁律是「判离线只认确定性证据」——那条当时只落在 _fetch_loop 里,
启动路径漏了。本次补齐。

改法(四处,全部在 account_manager.py,server.py 只加一行日志):
  A. worker 记 _start_error:异常不再只写日志,调用方能判断"是没开起来还是未登录"
  B. AccountManager 加 _shutting_down:停机开始后不许再启动账号、不许改运行意图
  C. start_account 的 else 分支:_verify_result != "no" 一律不算未登录(不落盘 offline)
  D. restore_running_accounts:停机即收手;只有确定性未登录才 discard 意图
  E. stop_all:把「正在启动中」的 worker 也纳入清理(以前它们完全没人管)
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"
S = r"D:\SPHGJ\backend\server.py"

PATCHES = []

# ---------------- A1. worker 加 _start_error 字段 ----------------
PATCHES.append((
    """        self._verify_result = None
        self.last_scan_at = None""",
    """        self._verify_result = None
        # rev60: 浏览器启动失败的原因。以前 _open_context() 抛的异常被 start() 吞成
        # 一行 ERROR 就完事,调用方拿不到任何信号 —— 只能看 logged_in=False,于是把
        # 「停机把浏览器砍了」当成「未登录」,把好账号落盘成 offline。
        self._start_error = None
        self.last_scan_at = None""",
    "A1 worker._start_error",
))

# ---------------- A2. worker.start 记录异常 ----------------
PATCHES.append((
    """        try:
            await self._open_context()
        except Exception as e:
            logger.error(f"[{self.account['id']}] 启动开浏览器失败: {e}")
        self._running = True""",
    """        try:
            await self._open_context()
        except Exception as e:
            logger.error(f"[{self.account['id']}] 启动开浏览器失败: {e}")
            # rev60: 记下来给调用方判断。注意这【不是】登录失效:launch / goto /
            # wait_for_timeout 任何一步抛异常时 _verify_result 都会停在 None
            # (rev54 约定:只认 "no",None = 没验成)。
            self._start_error = f"{type(e).__name__}: {str(e).splitlines()[0][:140]}"
        else:
            self._start_error = None
        self._running = True""",
    "A2 worker.start 记异常",
))

# ---------------- B. AccountManager 加停机标志与启动中登记 ----------------
PATCHES.append((
    """        self._playwright = None
        self._running = False
        self._fetch_lock = None          # 评论抓取全局串行锁(间隔排队)""",
    """        self._playwright = None
        self._running = False
        # rev60: 停机中标志。stop_all() 一开始就置 True,之后:
        #   · start_account / restore_running_accounts 立刻停手(不再拉新的 Chromium)
        #   · _remember_offline 拒绝落盘(停机时写的"未登录"几乎必然是假的)
        #   · 运行意图一个字都不许动(用户「让它跑」的意愿不能被一次重启吞掉)
        self._shutting_down = False
        # rev60: 正在 start 过程中、还没进 self.workers 的 worker。
        # 以前这类 worker 完全没人管:stop_all 只看 workers(那时是空的),
        # 却照样 playwright.stop() 一刀切,把它们正在做的 goto 砍成
        # 「Target page…closed」,再被误判成未登录。
        self._starting = {}              # account_id -> AccountWorker
        self._fetch_lock = None          # 评论抓取全局串行锁(间隔排队)""",
    "B manager 停机标志",
))

# ---------------- C. start_account:停机早退 + 登记 starting + else 分支区分 ----------------
PATCHES.append((
    """    async def start_account(self, account_id):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        if account_id in self.workers:
            return self.workers[account_id]""",
    """    async def start_account(self, account_id):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        if account_id in self.workers:
            return self.workers[account_id]
        # rev60: 正在停机就别再开新浏览器了 —— 开了也会被下面 stop_all 砍掉,
        # 砍掉还会被误判成未登录。这条路径只在「停机与恢复/自动发布撞车」时触发。
        if self._shutting_down:
            logger.info(f"[{account_id}] 正在停机,跳过启动(运行意图保留)")
            return None""",
    "C1 start_account 停机早退",
))

PATCHES.append((
    """        w._fetch_lock = self._fetch_lock
        w._cycle_offset = idx * self.config.get("fetch_interval_sec", 600) / total
        await w.start(self._playwright, headless=True)""",
    """        w._fetch_lock = self._fetch_lock
        w._cycle_offset = idx * self.config.get("fetch_interval_sec", 600) / total
        # rev60: 登记「启动中」,让 stop_all 能一起收拾它(见 B 段注释)
        self._starting[account_id] = w
        try:
            await w.start(self._playwright, headless=True)
        finally:
            self._starting.pop(account_id, None)""",
    "C2 登记 _starting",
))

PATCHES.append((
    """        else:
            # rev55: 启动时 auth_data 硬验就判「未登录」—— 这是确定性证据,必须记住。""",
    """        elif w._verify_result != "no":
            # rev60: ★ 关键分支。走到这里说明 logged_in=False,但【没验成】:
            # launch / goto / wait_for_timeout 抛了异常(典型:重启把浏览器砍了,
            # profile 锁被占,网络不通),_verify_result 停在 None。
            # 旧代码不看这个,一律 _remember_offline 落盘 offline —— 后果是
            #   ① data/auth_state.json 把好账号记成掉线,下次启动直接跳过、排队扫码;
            #   ② restore_running_accounts 见 aid 不在 workers,把运行意图也扔掉。
            # 一个"重启撞上正在恢复"就白丢一个账号,用户看到的是"好号突然要重扫码"。
            logger.warning(f"[{account_id}] 启动未完成({w._start_error or '浏览器未就绪'}),"
                           f"这不是登录失效,不记账、保留运行意图,下次再试")
            try:
                await w.stop()
            except Exception as e:
                logger.debug(f"[{account_id}] 清理未完成的 worker 异常(忽略): {e}")
        else:
            # rev55: 启动时 auth_data 硬验就判「未登录」—— 这是确定性证据,必须记住。""",
    "C3 else 分支区分",
))

# ---------------- D. _remember_offline 停机时拒绝落盘 ----------------
PATCHES.append((
    """        self._auth_offline[account_id] = {
            "reason": reason or "",
            "ts": time.time(),
            "fp": auth_fingerprint(acc) if acc else "",
        }
        self._save_auth_offline()""",
    """        # rev60: 停机期间写的"未登录"几乎必然是假证据(浏览器是被我们自己关的),
        # 落盘只会污染 auth_state.json,让下次启动白白跳过这个好账号。
        if self._shutting_down:
            logger.info(f"[{account_id}] 正在停机,跳过离线记账({reason})")
            return
        self._auth_offline[account_id] = {
            "reason": reason or "",
            "ts": time.time(),
            "fp": auth_fingerprint(acc) if acc else "",
        }
        self._save_auth_offline()""",
    "D _remember_offline 拦截",
))

# ---------------- E. restore_running_accounts 重写循环 ----------------
PATCHES.append((
    """        logger.info(f"[恢复] 重启后自动恢复 {len(todo)} 个关闭前在跑的账号: {todo}")
        for i, aid in enumerate(todo):
            if i:
                await asyncio.sleep(gap_sec)
            try:
                await self.start_account(aid)
                if aid not in self.workers:
                    # 未登录/启动失败:移出 intent,避免下次重启再空试
                    self._run_intent.discard(aid)
                    self._save_run_intent()
                    logger.warning(f"[恢复] {aid} 未登录或启动失败,已移出恢复列表")
            except Exception as e:
                logger.warning(f"[恢复] {aid} 启动异常(保留 intent,下次重启再试): {e}")
        logger.info("[恢复] 自动恢复流程结束")""",
    """        logger.info(f"[恢复] 重启后自动恢复 {len(todo)} 个关闭前在跑的账号: {todo}")
        for i, aid in enumerate(todo):
            # rev60: 一发现要停机就立刻收手,并把「还剩几个没恢复」写清楚。
            # 运行意图一个都不动 —— 下次启动接着恢复(以前是在这里把账号扔掉的)。
            if self._shutting_down:
                logger.warning(f"[恢复] 正在停机,中止自动恢复(已跳过 {len(todo) - i} 个,"
                               f"运行意图保留,下次启动继续)")
                return
            if i:
                # 停机时不能傻睡 gap_sec:分段等,保证能及时收手
                _left = float(gap_sec)
                while _left > 0 and not self._shutting_down:
                    _step = min(0.2, _left)
                    await asyncio.sleep(_step)
                    _left -= _step
                if self._shutting_down:
                    logger.warning(f"[恢复] 正在停机,中止自动恢复(已跳过 {len(todo) - i} 个,"
                                   f"运行意图保留,下次启动继续)")
                    return
            try:
                w = await self.start_account(aid)
                if aid in self.workers:
                    continue
                # rev60: 只有「auth_data 硬验明确说不」才算未登录、才踢出恢复列表。
                # 以前只要 aid 不在 workers 就 discard —— 「浏览器没开起来」(停机打断/
                # profile 锁/网络)也被当成未登录,把好账号的运行意图一起扔掉。
                if w is not None and getattr(w, "_verify_result", None) == "no":
                    self._run_intent.discard(aid)
                    self._save_run_intent()
                    logger.warning(f"[恢复] {aid} 确认未登录,已移出恢复列表(需重新扫码)")
                else:
                    _why = getattr(w, "_start_error", None) or "浏览器未就绪"
                    logger.warning(f"[恢复] {aid} 本次没启动成功({_why}),"
                                   f"保留运行意图,下次启动再试")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[恢复] {aid} 启动异常(保留 intent,下次重启再试): {e}")
        logger.info("[恢复] 自动恢复流程结束")""",
    "E restore 循环重写",
))

# ---------------- F. stop_all:置标志 + 收拾「启动中」的 worker ----------------
PATCHES.append((
    """        await self.stop()
        for s in list(self.login_sessions.values()):""",
    """        # rev60: 先立停机标志 —— 之后任何 start_account / _remember_offline /
        # 恢复循环都会立刻收手,不会再产生「被砍掉的开浏览器」和假的未登录记录。
        self._shutting_down = True
        self._running = False
        # rev60: 把「正在启动中」(还没进 self.workers)的 worker 也关掉。
        # 以前这批人完全没人管:stop_all 只看到 workers(常是空的)就报「账号进程 0 个」,
        # 紧接着 playwright.stop() 一刀切,把它们进行中的 goto/wait 砍成
        # 「Target page…closed」-> 被误判未登录 -> 踢出恢复列表。
        starting = [w for w in list(self._starting.values()) if w is not None]
        if starting:
            logger.info(f"[shutdown] 关闭 {len(starting)} 个正在启动中的账号进程")
            await asyncio.gather(*(w.stop() for w in starting), return_exceptions=True)
        self._starting.clear()
        await self.stop()
        for s in list(self.login_sessions.values()):""",
    "F stop_all 收拾启动中",
))

# ---------------- F2. 允许引擎被重新拉起时复位标志 ----------------
PATCHES.append((
    """    async def start(self, headless=True, auto_relogin=True):
        await self._ensure_playwright()
        self._running = True""",
    """    async def start(self, headless=True, auto_relogin=True):
        await self._ensure_playwright()
        # rev60: 引擎被重新拉起时复位停机标志(进程内 stop_all 之后就退出了,
        # 这条主要用于测试与「停机后又有人 start」的极端情况,避免永久卡死)
        self._shutting_down = False
        self._running = True""",
    "F2 start 复位标志",
))


def apply():
    s = io.open(P, encoding="utf-8").read()
    for old, new, tag in PATCHES:
        n = s.count(old)
        assert n == 1, f"{tag}: 命中 {n} 次(应为 1)"
        s = s.replace(old, new, 1)
        print(f"  ok  {tag}")
    io.open(P, "w", encoding="utf-8", newline="").write(s)

    # server.py:清理日志带上「启动中」
    t = io.open(S, encoding="utf-8").read()
    old = ("""    logger.info(f"[shutdown] 开始清理({_shutdown['path']}):账号进程 "
                f"{len(getattr(manager, 'workers', None) or {})} 个")""")
    new = ("""    # rev60: 以前只报 workers(常是 0 个),看不出「还在恢复过程中」这件事,
    # 于是「清理完成,账号进程 0 个」看起来像没问题,实际有账号正启动到一半被砍。
    logger.info(f"[shutdown] 开始清理({_shutdown['path']}):账号进程 "
                f"{len(getattr(manager, 'workers', None) or {})} 个,"
                f"启动中 {len(getattr(manager, '_starting', None) or {})} 个")""")
    assert t.count(old) == 1, f"server 日志命中 {t.count(old)} 次"
    io.open(S, "w", encoding="utf-8", newline="").write(t.replace(old, new, 1))
    print("  ok  G server 清理日志")


def selfcheck():
    a = io.open(P, encoding="utf-8").read()
    b = io.open(S, encoding="utf-8").read()
    checks = [
        ("worker 有 _start_error 字段", "self._start_error = None" in a),
        ("start() 记录异常", 'self._start_error = f"{type(e).__name__}' in a),
        ("manager 有 _shutting_down", "self._shutting_down = False" in a),
        ("manager 有 _starting", "self._starting = {}" in a),
        ("start_account 停机早退", "正在停机,跳过启动(运行意图保留)" in a),
        ("start_account 登记启动中", "self._starting[account_id] = w" in a),
        ("else 分支区分 verify_result", 'elif w._verify_result != "no":' in a),
        ("未完成不记账", "这不是登录失效,不记账、保留运行意图" in a),
        ("_remember_offline 拦截", "正在停机,跳过离线记账" in a),
        ("恢复循环停机收手", "正在停机,中止自动恢复" in a),
        ("只认 no 才 discard", 'if w is not None and getattr(w, "_verify_result", None) == "no":' in a),
        ("stop_all 置标志", "self._shutting_down = True" in a),
        ("stop_all 收拾启动中", "关闭 {len(starting)} 个正在启动中的账号进程" in a),
        ("start 复位标志", "self._shutting_down = False\n        self._running = True" in a),
        ("旧逻辑已消失(无条件 discard)", "未登录或启动失败,已移出恢复列表" not in a),
        ("server 日志带启动中", "'_starting', None" in b),
    ]
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print()
    print("SELFCHECK " + ("PASS" if not bad else "FAIL: " + ", ".join(bad)))
    return not bad


if __name__ == "__main__":
    print("=== 应用补丁 ===")
    apply()
    print("\n=== 自校验 ===")
    import sys
    sys.exit(0 if selfcheck() else 1)
