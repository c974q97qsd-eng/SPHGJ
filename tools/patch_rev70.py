# -*- coding: utf-8 -*-
"""rev70 一次性补丁:①worker 主路径补 localStorage 注入 ②300342 降级软故障+指数退避
③直播纯 HTTP 遇 300342 退避 1→2→4→8 分钟 ④登录态事件档案落地。

按铁律:同文件多处改动一次性打完,逐条 assert count==1,写完自校验。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AM = os.path.join(ROOT, "backend", "account_manager.py")
LF = os.path.join(ROOT, "backend", "live_fetcher.py")
DY = os.path.join(ROOT, "backend", "douyin", "worker.py")

P = []

# ---------------- account_manager.py ----------------

# P1 import
P.append((AM, "import-auth_events",
"""from .selectors import COMMENT_URL, POST_CREATE_URL
from .memtrim import MemoryTrimmer, trim_now""",
"""from .selectors import COMMENT_URL, POST_CREATE_URL
from .memtrim import MemoryTrimmer, trim_now
from . import auth_events"""))

# P2 __init__ 软故障状态
P.append((AM, "init-soft",
"""        self._http_last_why = ""        # 最近一次 HTTP 探测不可用的原因(no_spec/no_cookie/expired…)""",
"""        self._http_last_why = ""        # 最近一次 HTTP 探测不可用的原因(no_spec/no_cookie/expired…)
        # rev70: 软故障退避(300342 会话被新登录顶替)。这不是失效,不能判离线 ——
        # 但连着撞也没意义,按 60→120→240… 指数退避,避免每轮都白白撞一发。
        self._soft_fail_streak = 0
        self._soft_block_until = 0.0"""))

# P3 _open_context 注入 cookie + localStorage
P.append((AM, "open-context-inject",
"""        try:
            _ss = (self.account.get("storage_state") or {})
            _ck = _ss.get("cookies") or []
            if _ck:
                await self.context.add_cookies(_ck)
                logger.info(f"[{self.account['id']}] 已注入登录态 cookies={len(_ck)} 条")
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 注入登录态失败(回退用 profile 磁盘): {e}")""",
"""        # rev70: **cookie + localStorage 必须一起注**。视频号的登录身份
        # (__ml::aid / finder_username)存在 localStorage,只 add_cookies 页面照样当
        # 未登录:要么被弹回 login.html,要么 _aid 缺失让接口报 300333/300330 假过期。
        # rev66 给「另起浏览器」三个入口补过这一课,worker 主路径这里一直漏着。
        _ss = (self.account.get("storage_state") or {})
        _inj = await inject_login_state(self.context, _ss, self.account["id"], "worker")
        if not _inj and (_ss.get("cookies") or _ss.get("origins")):
            logger.warning(f"[{self.account['id']}] 注入登录态未成功"
                           f"(cookies={len(_ss.get('cookies') or [])} "
                           f"origins={len(_ss.get('origins') or [])}),回退用 profile 磁盘")"""))

# P4 _open_context_light 注入
P.append((AM, "light-inject",
"""        try:
            _ck = ((self.account.get("storage_state") or {}).get("cookies") or [])
            if _ck:
                await ctx.add_cookies(_ck)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 轻量探测注入登录态失败: {e}")""",
"""        # rev70: 同样 cookie + localStorage 一起注 —— ctx.request 虽然只吃 cookie,
        # 但 ctx 之后可能被复用成页面(大屏/串流),留着半套登录态迟早出事。
        await inject_login_state(ctx, self.account.get("storage_state"),
                                 self.account["id"], "probe")"""))

# P5a _probe_via_http 入口:软故障退避也生效
P.append((AM, "probe-entry",
"""        if time.time() < self._http_block_until:
            return None""",
"""        _now = time.time()
        if _now < self._http_block_until or _now < self._soft_block_until:
            return None"""))

# P5b 成功:清软故障
P.append((AM, "probe-ok",
"""        if res is not None:
            self._http_block_until = 0.0
            self._http_block_logged = ""
            return res""",
"""        if res is not None:
            self._http_block_until = 0.0
            self._http_block_logged = ""
            if self._soft_fail_streak:
                # rev70: 被顶之后自己恢复了(新登录把会话换回来了 / 对方下线)
                logger.info(f"[{self.account['id']}] HTTP 直连已恢复"
                            f"(此前连续 {self._soft_fail_streak} 次被顶),清除退避")
                auth_events.record(self.account["id"], "soft_recover",
                                   reason="HTTP 直连恢复", source="probe_http",
                                   extra={"streak": self._soft_fail_streak})
                self._soft_fail_streak = 0
            self._soft_block_until = 0.0
            return res"""))

# P5c 失败:300342 -> 软故障指数退避
P.append((AM, "probe-kicked",
"""        why = diag.get("why") or ""
        self._http_last_why = why
        if why in ("no_cookie", "badauth", "expired"):""",
"""        why = diag.get("why") or ""
        self._http_last_why = why
        if why == "kicked":
            # rev70: errCode=300342 = 会话正被【别处的登录】顶替 —— 软故障,不是失效。
            # 判定上绝不据此判离线(浏览器 auth_data 硬验才算数);但连着撞没意义,
            # 60→120→240→480→900 秒指数退避,期间直接走浏览器路径。
            self._soft_fail_streak += 1
            n = self._soft_fail_streak
            wait = min(60 * (2 ** (n - 1)), 900)
            self._soft_block_until = time.time() + wait
            if n <= 3 or n % 10 == 0:
                logger.warning(f"[{self.account['id']}] HTTP 探测被顶"
                               f"(errCode={diag.get('code')} 会话正被别处登录顶替),"
                               f"连续第 {n} 次 -> {wait}s 内不再走 HTTP 直连;"
                               f"软故障,不判离线(以浏览器硬验为准)")
            if n == 1:
                auth_events.record(self.account["id"], "http_kicked",
                                   code=diag.get("code"),
                                   reason="会话正被别处登录顶替",
                                   source="probe_http",
                                   extra={"streak": n, "backoff_sec": wait})
            self._http_block_until = 0.0
            return None
        if why in ("no_cookie", "badauth", "expired"):"""))

# P5d 硬故障分支记档案(去重:同一原因只记一次)
P.append((AM, "probe-hard-event",
"""            if self._http_block_logged != why:
                self._http_block_logged = why
                if why == "no_cookie":""",
"""            if self._http_block_logged != why:
                self._http_block_logged = why
                auth_events.record(
                    self.account["id"],
                    "http_expired" if why == "expired" else
                    ("http_nocookie" if why == "no_cookie" else "http_badauth"),
                    code=diag.get("code"), reason=why, source="probe_http")
                if why == "no_cookie":"""))

# P6a mark_offline 记档案
P.append((AM, "offline-event",
"""        await self._emit("account_offline", {"account_id": self.account["id"],
                                            "reason": self.auth_reason,
                                            "code": code, "ts": self.auth_ts})""",
"""        auth_events.record(self.account["id"], "offline", code=code,
                           reason=self.auth_reason, source="mark_offline")
        await self._emit("account_offline", {"account_id": self.account["id"],
                                            "reason": self.auth_reason,
                                            "code": code, "ts": self.auth_ts})"""))

# P6b mark_online 记档案 + 清软故障
P.append((AM, "online-event",
"""        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""
        try:""",
"""        self._http_block_until = 0.0
        self._http_block_logged = ""
        self._http_last_why = ""
        # rev70: 重新上线 = 之前所有退避作废(用户可能刚重新扫码)
        self._soft_fail_streak = 0
        self._soft_block_until = 0.0
        if was == "offline" or why:
            auth_events.record(self.account["id"], "online", reason=why or "",
                               source="mark_online",
                               extra={"was": was})
        try:"""))

# ---------------- live_fetcher.py ----------------

# P7 init 退避状态
P.append((LF, "lf-init",
"""        self._http_last_why = ""        # rev69: 纯 HTTP 最近一次失败原因(诊断用)""",
"""        self._http_last_why = ""        # rev69: 纯 HTTP 最近一次失败原因(诊断用)
        # rev70: 纯 HTTP 连续被顶(300342)。账号会话被别处登录顶替时,继续每秒一枪
        # 只会把日志刷满、还拖慢真正有用的轮询 —— 退避 1→2→4→8 分钟(封顶 8 分钟)。
        # 注意:退避只停【接口轮询】,已经推给前端的 flv 流不受影响(不关流、不下播)。
        self._http_kick_streak = 0
        self._http_backoff_until = 0.0"""))

# P8a _poll_http 开头:退避中直接返回快照
P.append((LF, "lf-poll-entry",
"""        if not self._learned:
            self._learned = _load_spec_cache(self.account_id) or {}
        got = False""",
"""        # rev70: 退避期内直接返回上一份快照 —— 一个包都不发。
        if time.time() < self._http_backoff_until:
            return await self.snapshot()
        if not self._learned:
            self._learned = _load_spec_cache(self.account_id) or {}
        got = False"""))

# P8b _poll_http 失败分支:300342 退避
P.append((LF, "lf-poll-kick",
"""        if got:
            self._poll_fail_streak = 0
        else:
            self._poll_fail_streak += 1
            n = self._poll_fail_streak
            if n in (3, 10) or n % 60 == 0:
                logger.warning(f"[live:{self.account_id}] 纯 HTTP 轮询连续失败 {n} 次"
                               f"(规格={sorted(self._learned)}, "
                               f"why={getattr(self, '_http_last_why', '')})")""",
"""        if got:
            self._poll_fail_streak = 0
            self._http_kick_streak = 0
            self._http_backoff_until = 0.0
        else:
            self._poll_fail_streak += 1
            n = self._poll_fail_streak
            why = getattr(self, "_http_last_why", "") or ""
            if "300342" in why:
                # rev70: 会话被别处登录顶替。软故障 —— 不判离线、不关流,
                # 只把接口轮询按 1/2/4/8 分钟往后推;顶替结束后第一枪成功即恢复。
                self._http_kick_streak += 1
                k = self._http_kick_streak
                wait = min(60 * (2 ** (k - 1)), 480)
                self._http_backoff_until = time.time() + wait
                if k <= 2 or k % 5 == 0:
                    logger.warning(f"[live:{self.account_id}] 纯 HTTP 轮询被顶"
                                   f"(errCode=300342)连续第 {k} 次,"
                                   f"{wait}s 内暂停接口轮询"
                                   f"(视频流不受影响,不判离线)")
            elif n in (3, 10) or n % 60 == 0:
                logger.warning(f"[live:{self.account_id}] 纯 HTTP 轮询连续失败 {n} 次"
                               f"(规格={sorted(self._learned)}, why={why})")"""))

# ---------------- douyin/worker.py ----------------

# P9 抖音 worker 也补 localStorage
P.append((DY, "dy-inject",
"""        try:
            ss = self.account.get("_dy_storage_state") or {}
            ck = ss.get("cookies") or []
            if ck:
                await self.context.add_cookies(ck)
                logger.info("[dy:%s] 已注入登录态 cookies=%d 条" % (self.account_id, len(ck)))
        except Exception as e:
            logger.warning("[dy:%s] 注入登录态失败(回退用 profile 磁盘): %r"
                           % (self.account_id, e))""",
"""        # rev70: cookie + localStorage 一起注(与视频号 worker 同一课:只注 cookie
        # 页面照样当未登录)。抖音身份存 localStorage 的 odin_tt / sessionid 辅助键。
        try:
            ss = self.account.get("_dy_storage_state") or {}
            from ..local_window import inject_login_state
            if not await inject_login_state(self.context, ss, self.account_id, "dy"):
                if ss:
                    logger.warning("[dy:%s] 注入登录态未成功(回退用 profile 磁盘)"
                                   % self.account_id)
        except Exception as e:
            logger.warning("[dy:%s] 注入登录态失败(回退用 profile 磁盘): %r"
                           % (self.account_id, e))"""))


def apply():
    files = {}
    for path, tag, old, new in P:
        s = files.get(path)
        if s is None:
            s = io.open(path, encoding="utf-8").read()
        cnt = s.count(old)
        if cnt != 1:
            print("FAIL[%s] 锚点命中 %d 次: %r" % (tag, cnt, old[:60]))
            return False
        s = s.replace(old, new, 1)
        files[path] = s
    for path, s in files.items():
        backup = path + ".bak-r70"
        if not os.path.exists(backup):
            io.open(backup, "w", encoding="utf-8", newline="").write(
                io.open(path, encoding="utf-8").read())
        io.open(path, "w", encoding="utf-8", newline="").write(s)
        print("written:", path)
    return True


if __name__ == "__main__":
    sys.exit(0 if apply() else 1)
