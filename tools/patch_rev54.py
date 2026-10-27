# -*- coding: utf-8 -*-
"""rev54 补丁:账号在网状态机 —— 判定掉线后不再发包,直到扫码登录成功才回到轮询。

用户 2026-10-28 的要求:
    「现在每轮会先试 HTTP,只要你掉线状态是准确的,掉线状态的账号就不用去发包了啊,
      直到扫码登录后才加入轮询任务。」

现状问题(为什么会有这个补丁):
  1. http_available() 只看 config.storage_state 里【有没有 cookie】,不看还有没有效。
     6/9/10 号那种 cookie 已过期(300333)的账号,每轮都先发一次包再被拒。
  2. 1/7 号 storage_state 为空 -> 走 ensure_browser 路径,每轮白开一次 Chromium
     (8~15s 启动 + goto + auth 硬验),下一轮再来一遍。
  3. 更糟:未登录分支返回 (None, False),finally 里不执行 release_idle_browser,
     引用计数每轮泄漏 +1 -> context 永不关闭,掉线账号常驻 Chromium(母本内存压力来源)。

改法:
  - AccountWorker 增加 auth_state(unknown/ok/offline) 状态机 + 跨重启持久化。
  - 判离线的证据必须【确定】:HTTP errCode 300333/300330(sessionid 失效/登录态过期)
    且回退到真浏览器后 auth_data 硬验仍给不出登录身份。其它错误(-1/超时/300342)
    一律不判 —— 它们会自己恢复,误判会把好账号踢出轮询。
  - 离线后:_fetch_loop / _live_detect_loop 只等事件,不抢锁、不发包、不开浏览器;
    自动排队推二维码;扫码成功(start_account / relogin finalized)后 mark_online 复位。
  - 顺手修引用计数泄漏:ensure_browser 失败也要走 release。
  - 顺手回填 storage_state:浏览器硬验通过后把现取会话写回 config,
    让「HTTP cookie 死了但 profile 磁盘还活着」的账号下一轮就恢复 HTTP 直连。

用法:python tools/patch_rev54.py   (幂等,重复跑会报已打过)
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "backend", "api_client.py")
AM = os.path.join(ROOT, "backend", "account_manager.py")

PATCHES = []


def P(path, old, new, tag):
    PATCHES.append((path, old, new, tag))


# ============================== api_client.py ==============================
P(API, '''        self._daily_write_limit = int(rc.get("daily_write_limit", 100))
''', '''        self._daily_write_limit = int(rc.get("daily_write_limit", 100))
        # rev54: 登录态被服务端【明确】判死(errCode 300333/300330)时置位。
        # 与 _http_block_until 的区别:那个只是"先别试 180s",这个是"cookie 已经死了",
        # 只有重新登录、或浏览器现取到新会话回填 storage_state 之后才清。
        # 上层(AccountWorker)据此把账号判为掉线并退出轮询 —— 不再发包。
        self.auth_fail = None
''', "api:init")

P(API, '''        if time.time() < self._http_block_until:
            return False
        if not is_write:
            return self._read_via == "http"
''', '''        if time.time() < self._http_block_until:
            return False
        # rev54: 这一轮已经撞到 300333/300330(cookie 已死),后面别再逐个请求去撞
        if not is_write and self.auth_fail:
            return False
        if not is_write:
            return self._read_via == "http"
''', "api:via_http")

P(API, '''        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333),
                                      is_write=is_write)
''', '''        code = self._err_code(resp)
        if code not in (0, None):
            # 300333 = sessionid 无效 / 登录态过期;300330 = sessionid 无效。
            # rev54: 这不是限流,是【登录态死了】—— 记下来给上层判掉线用。
            if code in (300333, 300330):
                self.auth_fail = {"code": code, "ts": time.time(), "path": path}
            # 读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code in (300333, 300330)),
                                      is_write=is_write)
''', "api:err_code")

P(API, '''        if not self._http_logged:
            self._http_logged = True
''', '''        # 有一次成功就说明会话是活的,清掉失效标记
        self.auth_fail = None
        if not self._http_logged:
            self._http_logged = True
''', "api:success")

P(API, '''        if time.time() < self._http_block_until:
            return False
        try:
            return bool(wx_http.cookie_header_of(self._latest_account()))
''', '''        if time.time() < self._http_block_until:
            return False
        # rev54: cookie 已被服务端判死时,不要再拿它去发包白撞一次
        if self.auth_fail:
            return False
        try:
            return bool(wx_http.cookie_header_of(self._latest_account()))
''', "api:http_available")

P(API, '''    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
''', '''    def clear_auth_fail(self):
        """清除登录态失效标记(rev54)。重新登录 / 浏览器现取到新会话后调用。"""
        self.auth_fail = None
        self._http_block_until = 0.0

    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
''', "api:clear_auth_fail")

# ============================ account_manager.py ============================
P(AM, '''import os
import json
import time
import random
''', '''import os
import json
import time
import random
import hashlib
''', "am:import")

P(AM, '''def _in_night_hours(night_hours):
''', '''def auth_fingerprint(account):
    """登录态指纹 —— 只取 cookie 的 name=value 串(与 wx_http 取 cookie 的口径一致)。

    rev54: 用来判断「上次判离线之后,登录态有没有变过」。变了 = 用户重新扫码了,
    离线记录自动作废;没变 = 别重启一次又开一遍浏览器白验。
    空串表示本地压根没有可用 cookie(从未登录 / storage_state 被清空)。
    """
    try:
        h = build_cookie_header((account or {}).get("storage_state")) or ""
    except Exception:
        h = ""
    return hashlib.md5(h.encode("utf-8")).hexdigest() if h else ""


def _in_night_hours(night_hours):
''', "am:fingerprint")

P(AM, '''from .live_fetcher import LiveFetcher, judge_live, _load_spec_cache
''', '''from .live_fetcher import LiveFetcher, judge_live, _load_spec_cache
from .wx_http import build_cookie_header
''', "am:import2")

P(AM, '''        self.logged_in = False
        self.last_scan_at = None
''', '''        self.logged_in = False
        # ===== rev54: 账号在网状态机 =====
        # unknown: 还没定论(刚建 worker);ok: 确定可用;offline: 登录态已失效。
        # 为什么要它:掉线账号原来的行为是「每轮白试一次 HTTP(300333)-> 回退浏览器
        # -> 开 Chromium 硬验 -> 未登录跳过」,下一轮再来一遍。用户 2026-10-28 要求:
        # 掉线判定要准确,定了就【不再发任何包】,直到扫码登录成功才回到轮询。
        self.auth_state = "unknown"
        self.auth_reason = ""
        self.auth_code = None
        self.auth_ts = 0.0
        self._online_event = asyncio.Event()
        self._online_event.set()          # 默认放行;判离线时 clear
        self.on_auth_change = None        # async (account_id, state, reason) -> None
        # 最近一次 auth_data 硬验的结论:"ok"/"no"/None(没验过/验不出)。判离线只认 "no"
        self._verify_result = None
        self.last_scan_at = None
''', "am:worker_init")

P(AM, '''    # ======================== 按需浏览器生命周期(省内存) ========================

    # ============== 登录态真伪校验(rev37) ==============
''', '''    # ======================== 按需浏览器生命周期(省内存) ========================

    # ======================== rev54: 账号在网状态机 ========================

    @property
    def is_offline(self):
        return self.auth_state == "offline"

    async def mark_offline(self, reason, code=None):
        """判定登录态已失效:停轮询、停探测、关浏览器,不再向服务端发任何请求。

        证据必须是【确定性】的 —— HTTP 直连报 300333/300330,并且回退到真浏览器后
        auth_data 硬验仍给不出登录身份。其它错误(-1 / 超时 / 300342 会话被顶替)
        一律不判离线:那些要么会自己恢复,要么是重登窗口期的正常现象,误判会把
        好账号踢出轮询(300342 就是这么被误解成"限流"过的)。
        """
        if self.auth_state == "offline":
            if reason and reason != self.auth_reason:
                self.auth_reason = reason
            return
        self.auth_state = "offline"
        self.auth_reason = reason or ""
        self.auth_code = code
        self.auth_ts = time.time()
        self.logged_in = False
        self._online_event.clear()
        # 引用计数清零:掉线后不再有任何合法持有者。不清会让下次 ensure_browser 漏减,
        # context 永远关不掉(原来未登录分支每轮泄漏 +1 就是这么来的)。
        self._browser_refs = 0
        logger.warning(f"[{self.account['id']}] 判定登录态失效({self.auth_reason}"
                       + (f", errCode={code}" if code else "") +
                       f"),已退出轮询:不再发送任何请求,扫码登录成功后自动恢复")
        try:
            await self._close_context()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 掉线后关浏览器异常(忽略): {e}")
        await self._emit("account_offline", {"account_id": self.account["id"],
                                            "reason": self.auth_reason,
                                            "code": code, "ts": self.auth_ts})
        await self._notify_auth_change()

    async def mark_online(self, why=""):
        """登录态恢复可用:清失效标记,重新加入轮询(评论抓取 + 直播探测)。"""
        was = self.auth_state
        self.auth_state = "ok"
        self.auth_reason = ""
        self.auth_code = None
        self.auth_ts = time.time()
        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""
        try:
            if self.api is not None:
                self.api.clear_auth_fail()
        except Exception:
            pass
        self._online_event.set()
        if was != "ok":
            logger.info(f"[{self.account['id']}] 登录态可用"
                        + (f"({why})" if why else "") + ",已加入轮询")
            if self._running:
                self.start_loop()          # 内部幂等:已在跑不会重复起
                self.start_live_detect()
            await self._notify_auth_change()

    async def _notify_auth_change(self):
        cb = self.on_auth_change
        if cb is None:
            return
        try:
            await cb(self.account["id"], self.auth_state, self.auth_reason)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 在网状态回调异常(忽略): {e}")

    async def _wait_online(self, timeout=600.0):
        """离线等待:不抢抓取锁、不发包、不开浏览器。超时返回后由调用方重查状态。"""
        if self.auth_state != "offline":
            return
        try:
            await asyncio.wait_for(self._online_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _refresh_storage_state(self):
        """把浏览器现取的登录态回填 config.storage_state(rev54)。

        为什么必须做:cookie 的权威副本在浏览器里,config 里的 storage_state 只是
        扫码那一刻的快照,会逐渐过期。过期之后 HTTP 直连每次 300333 -> 每轮都得开
        一次 Chromium 才抓得动,白烧一次启停。这里在【硬验登录有效之后】顺手回填,
        指纹一变下一轮 HTTP 就恢复直连。

        只在 cookie 串真的变了才写盘,避免每轮重写 config.json。
        """
        if self.context is None:
            return False
        try:
            st = await self.context.storage_state()
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 导出登录态失败: {e}")
            return False
        if not (st or {}).get("cookies"):
            return False
        old = auth_fingerprint(self.account)
        self.account["storage_state"] = st
        if auth_fingerprint(self.account) == old:
            return False
        cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.json")
        try:
            tmp = cfg + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cfg)
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 回填登录态写盘失败: {e}")
            return False
        logger.info(f"[{self.account['id']}] 已用浏览器现取会话刷新登录态"
                    f"(cookies={len(st.get('cookies') or [])} 条),HTTP 直连可恢复")
        return True

    # ============== 登录态真伪校验(rev37) ==============
''', "am:state_machine")

P(AM, '''        if self.context is not None:
            return
        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
''', '''        if self.context is not None:
            return
        self._verify_result = None
        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
''', "am:open_context_head")

P(AM, '''        self.logged_in = await self._verify_login()
        if self.logged_in:
            logger.info(f"[{self.account['id']}] 已登录")
''', '''        self.logged_in = await self._verify_login()
        # rev54: 记下硬验结论。判离线只认 "no" —— launch/goto 抛异常时这里是 None,
        # 那种情况只能算"没验成",不能当成"未登录"(否则网络抖动就把账号踢出轮询)
        self._verify_result = "ok" if self.logged_in else "no"
        if self.logged_in:
            logger.info(f"[{self.account['id']}] 已登录")
''', "am:verify_result")

P(AM, '''            self.logged_in = False
        # rev38: 登录态真实可用时补齐 login_at、清掉 logout_at。
''', '''            self.logged_in = False
            self._verify_result = "no"
        # rev38: 登录态真实可用时补齐 login_at、清掉 logout_at。
''', "am:lock_verify")

P(AM, '''        if self.logged_in:
            await self._sync_login_time()
        self.api = WxApiClient(self.page, self.account, self.config, self.storage)
''', '''        if self.logged_in:
            await self._sync_login_time()
            # rev54: 浏览器里的 cookie 才是权威副本 —— 顺手回填 config.storage_state,
            # 让 HTTP 直连的登录态跟着一起刷新,不用每轮都开浏览器。
            await self._refresh_storage_state()
            await self.mark_online("浏览器 auth_data 硬验通过")
        self.api = WxApiClient(self.page, self.account, self.config, self.storage)
''', "am:open_context_online")

P(AM, '''            if self.live_fetcher is not None and live_fetch_interval == 0:
                await asyncio.sleep(30)
                continue
            # 串行抓取:全局队列锁,保证多账号不同时拉起浏览器(省内存 + 防风控关联)
''', '''            if self.live_fetcher is not None and live_fetch_interval == 0:
                await asyncio.sleep(30)
                continue
            # rev54: 掉线账号【不进轮询】—— 不抢锁、不发包、不开浏览器,等 mark_online()
            # (扫码登录成功)唤醒。超时只是兜底重查一次状态,本身不产生任何请求。
            if self.auth_state == "offline":
                await self._wait_online(min(int(interval or 600), 600))
                continue
            # 串行抓取:全局队列锁,保证多账号不同时拉起浏览器(省内存 + 防风控关联)
''', "am:fetch_loop_guard")

P(AM, '''        返回 (5 元组, 是否用了浏览器);连登录态都拿不到时返回 (None, False)。
        """
        if self.fetcher is None:
            return None, False
''', '''        返回 (5 元组, 是否用了浏览器);连登录态都拿不到时返回 (None, False)。

        rev54: 已判掉线(auth_state=offline)的账号一个包都不发,直接返回 ——
        这是用户 2026-10-28 定的规则:掉线状态准确就够了,不需要每轮去验证一遍。
        """
        if self.fetcher is None:
            return None, False
        if self.auth_state == "offline":
            return None, False
''', "am:run_fetch_round_head")

P(AM, '''        ok = await self.ensure_browser()
        if not ok:
            logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败(未登录),跳过本轮")
            return None, False
''', '''        ok = await self.ensure_browser()
        if not ok:
            if self._verify_result == "no":
                # rev54: 这是「准确判定掉线」的完整证据链 —— HTTP 侧 cookie 不可用/
                # 被拒,回退到真浏览器之后 auth_data 硬验仍拿不到登录身份。
                code = None
                try:
                    code = (self.api.auth_fail or {}).get("code") if self.api else None
                except Exception:
                    code = None
                await self.mark_offline("未登录(HTTP 与浏览器硬验均无有效登录态)",
                                        code=code)
            else:
                # 开浏览器失败/硬验无结论 -> 只是本轮不成,不判离线(别把网络抖动当掉线)
                logger.warning(f"[{self.account['id']}] 抓取前开浏览器失败或无法判定登录态,"
                               f"跳过本轮(不判离线)")
            # 注意:这里必须返回 True。ensure_browser 已经 +1,不 release 就会每轮
            # 漏一个引用,context 永远关不掉(原来掉线账号常驻 Chromium 的根因)。
            return None, True
''', "am:run_fetch_round_browser")

P(AM, '''        hit = await self._probe_via_http()
        if hit is not None:
            return hit
''', '''        # rev54: 掉线账号不探测 —— 连 L1/L2 都不该走,那两个会开浏览器
        if self.auth_state == "offline":
            return None
        hit = await self._probe_via_http()
        if hit is not None:
            return hit
''', "am:probe_light_guard")

P(AM, '''                if self.live_fetcher is None and self.logged_in:
                    boost = time.time() < self._probe_boost_until
''', '''                if self.live_fetcher is None and self.logged_in and self.auth_state != "offline":
                    boost = time.time() < self._probe_boost_until
''', "am:detect_guard")

P(AM, '''        def _usable(w):
            if w.logged_in:
                return True
''', '''        def _usable(w):
            if getattr(w, "auth_state", "") == "offline":
                return False
            if w.logged_in:
                return True
''', "am:fetch_all_once")

P(AM, '''            "probe": self._probe_view(w),
''', '''            "probe": self._probe_view(w),
            # rev54: 在网状态。offline = 已判登录失效,该账号不再发任何请求,
            # 前端可以据此把卡片标成"待扫码"而不是"已登录"。
            "auth_state": getattr(w, "auth_state", "unknown") if w else "unknown",
            "auth_reason": getattr(w, "auth_reason", "") if w else "",
            "auth_offline_at": (getattr(w, "auth_ts", None)
                                if w and getattr(w, "auth_state", "") == "offline" else None),
''', "am:status")

P(AM, '''        self.remote_registry = None
''', '''        self.remote_registry = None
        # rev54: 账号在网状态持久化。掉线判定要跨重启保留 —— 否则每次重启都要
        # 「发包 -> 300333 -> 开浏览器 -> 未登录」把每个掉线账号再白跑一轮。
        self._auth_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "auth_state.json")
        self._auth_offline = self._load_auth_offline()
''', "am:manager_init")

P(AM, '''    # ---------- rev21: 运行意图持久化(重启恢复) ----------
''', '''    # ---------- rev54: 账号在网状态(掉线记录)持久化 ----------
    def _load_auth_offline(self) -> dict:
        """读取上次判定掉线的账号表 {acc_id: {reason, ts, fp}};损坏就返回空表。"""
        try:
            with open(self._auth_file, encoding="utf-8") as f:
                d = json.load(f) or {}
            return {k: v for k, v in d.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _save_auth_offline(self):
        try:
            os.makedirs(os.path.dirname(self._auth_file), exist_ok=True)
            tmp = self._auth_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._auth_offline, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._auth_file)
        except Exception as e:
            logger.warning(f"在网状态落盘失败(忽略): {e}")

    def _clear_auth_offline(self, account_id):
        if self._auth_offline.pop(account_id, None) is not None:
            self._save_auth_offline()

    def _is_known_offline(self, acc) -> bool:
        """该账号上次已判掉线,且登录态一个字节都没变 -> 不必再启动去白验一次。"""
        off = self._auth_offline.get(acc.get("id") or "")
        if not off:
            return False
        return off.get("fp") == auth_fingerprint(acc)

    async def _on_worker_auth(self, account_id, state, reason=""):
        """worker 在网状态变化:离线 -> 落盘 + 排队扫码;恢复 -> 清记录。"""
        if state == "offline":
            acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
            self._auth_offline[account_id] = {
                "reason": reason or "",
                "ts": time.time(),
                "fp": auth_fingerprint(acc) if acc else "",
            }
            self._save_auth_offline()
            await self._emit("engine_status", self.status_snapshot())
            # 自动推网页二维码:与启动时未登录账号走同一条串行队列,不会同时弹多个
            try:
                await self.enqueue_auto_relogin(account_id)
            except Exception as e:
                logger.warning(f"[{account_id}] 掉线后排队扫码失败: {e}")
        elif state == "ok":
            if self._auth_offline.pop(account_id, None) is not None:
                self._save_auth_offline()
                await self._emit("engine_status", self.status_snapshot())

    # ---------- rev21: 运行意图持久化(重启恢复) ----------
''', "am:manager_state")

P(AM, '''        self.workers[account["id"]] = worker
        return worker
''', '''        worker.on_auth_change = self._on_worker_auth
        self.workers[account["id"]] = worker
        return worker
''', "am:add_account_cb")

P(AM, '''        if account_id in self.workers:
            return self.workers[account_id]
''', '''        if account_id in self.workers:
            return self.workers[account_id]
        # rev54: 上次已判掉线,并且登录态没变过 -> 别再开一次浏览器去验,直接排队扫码。
        # 扫码成功后 storage_state 会变,指纹不匹配,这条分支自然失效。
        if self._is_known_offline(acc):
            logger.info(f"[{account_id}] 上次判定登录失效且登录态未更新,跳过启动,直接排队扫码")
            await self._auth_offline_on_start(account_id, acc)
            return None
''', "am:start_account_skip")

P(AM, '''        w = AccountWorker(acc, self.storage, self.config, self._emit)
        # 按账号在配置中的序号分配错峰偏移,与既有 worker 错开抓取节奏
''', '''        w = AccountWorker(acc, self.storage, self.config, self._emit)
        w.on_auth_change = self._on_worker_auth
        # 按账号在配置中的序号分配错峰偏移,与既有 worker 错开抓取节奏
''', "am:start_account_cb")

P(AM, '''        for acc in self.config.get("accounts", []):
            try:
                w = await self.add_account(acc, headless=headless)
''', '''        for acc in self.config.get("accounts", []):
            # rev54: 上次已判掉线且登录态没变 -> 不启动(省一次 Chromium 启停),
            # 直接进下面的 expired 列表去排队扫码
            if self._is_known_offline(acc):
                logger.info(f"账号 {acc['id']} 上次判定登录失效且登录态未更新,跳过启动")
                expired.append(acc["id"])
                continue
            try:
                w = await self.add_account(acc, headless=headless)
''', "am:start_skip")

P(AM, '''        if status == "finalized":
            # rev29: 等登录会话把 profile 彻底释放再启动。以前立刻 start,和会话收尾的
''', '''        if status == "finalized":
            # rev54: 扫码成功 = 登录态已经换新,掉线记录作废
            self._clear_auth_offline(account_id)
            # rev29: 等登录会话把 profile 彻底释放再启动。以前立刻 start,和会话收尾的
''', "am:relogin_clear")

# start_account 跳过分支要用的辅助(避免重复写两遍落盘逻辑)
P(AM, '''    async def start_account(self, account_id):
''', '''    async def _auth_offline_on_start(self, account_id, acc):
        """跳过启动时补记一次离线状态并排队扫码(rev54)。"""
        self._auth_offline[account_id] = {
            "reason": (self._auth_offline.get(account_id) or {}).get("reason", "登录态失效"),
            "ts": (self._auth_offline.get(account_id) or {}).get("ts", time.time()),
            "fp": auth_fingerprint(acc),
        }
        self._save_auth_offline()
        try:
            await self.enqueue_auto_relogin(account_id)
        except Exception as e:
            logger.warning(f"[{account_id}] 排队扫码失败: {e}")

    async def start_account(self, account_id):
''', "am:start_account_helper")


def apply():
    changed = 0
    for path, old, new, tag in PATCHES:
        with io.open(path, encoding="utf-8") as f:
            s = f.read()
        if new in s:
            print(f"  [skip] {tag} 已打过")
            continue
        n = s.count(old)
        if n != 1:
            print(f"  [FAIL] {tag} 命中 {n} 次(要求 1 次)")
            return False
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(s.replace(old, new, 1))
        changed += 1
        print(f"  [ok] {tag}")
    print(f"共应用 {changed} 处补丁")
    return True


def selfcheck():
    """自校验:每条改动的关键标志都必须真的在文件里。"""
    checks = [
        (API, "self.auth_fail = None"),
        (API, 'if code in (300333, 300330):'),
        (API, "def clear_auth_fail(self):"),
        (AM, "def auth_fingerprint(account):"),
        (AM, "from .wx_http import build_cookie_header"),
        (AM, 'self.auth_state = "unknown"'),
        (AM, "async def mark_offline(self, reason, code=None):"),
        (AM, "async def mark_online(self, why=\"\"):"),
        (AM, "async def _wait_online(self, timeout=600.0):"),
        (AM, "async def _refresh_storage_state(self):"),
        (AM, 'if self.auth_state == "offline":\n                await self._wait_online'),
        (AM, 'self._verify_result = "ok" if self.logged_in else "no"'),
        (AM, "def _load_auth_offline(self) -> dict:"),
        (AM, "async def _on_worker_auth(self, account_id, state, reason=\"\"):"),
        (AM, "def _is_known_offline(self, acc) -> bool:"),
        (AM, '"auth_state": getattr(w, "auth_state", "unknown") if w else "unknown",'),
        (AM, "worker.on_auth_change = self._on_worker_auth"),
        (AM, "w.on_auth_change = self._on_worker_auth"),
        (AM, 'if self._is_known_offline(acc):\n            logger.info(f"[{account_id}]'),
        (AM, "self._clear_auth_offline(account_id)"),
    ]
    bad = []
    for path, needle in checks:
        with io.open(path, encoding="utf-8") as f:
            if needle not in f.read():
                bad.append((os.path.basename(path), needle[:50]))
    if bad:
        for b in bad:
            print(f"  [SELFCHECK FAIL] {b[0]}: {b[1]}")
        return False
    print(f"  SELFCHECK PASS ({len(checks)} 项)")
    return True


if __name__ == "__main__":
    print("rev54 补丁:账号在网状态机")
    if not apply():
        sys.exit(1)
    if not selfcheck():
        sys.exit(1)
    # 语法自检
    import py_compile
    for p in (API, AM):
        py_compile.compile(p, doraise=True)
    print("语法检查通过")
