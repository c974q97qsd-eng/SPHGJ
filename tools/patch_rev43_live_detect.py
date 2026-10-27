# -*- coding: utf-8 -*-
"""rev43: 修「直播检测漏判」——学习窗口与判定窗口错配 + 规格不持久

现象(2026-10-28 01:24 母本 2 号):
    01:24:26 已登录
    01:24:44 [live:uspoloassn2] 直播检测回退到 CDP 响应捕获(主动轮询不可用)
    01:24:48 [uspoloassn2] 直播检测:当前未在直播(live_stats=False, live_object_id=None)

根因三处:
  1) poll() 的 `_learning` 是"全有或全无"开关 —— 15s 内必须同时学到 get_live_info
     和 check_live_status 才肯主动轮询;只学到其中一个就掉进 CDP 被动捕获,手上明明
     有一份可用 spec 却不用。
  2) 回退 CDP 时【不 reload】。responseReceived 只能看到【之后】发出的响应,而
     check_live_status 在页面初始化时就发完了 -> 监听等于空等,必然误判下播。
  3) 学到的 spec 只活在内存,context 一回收(live_page 定期重建 / 省内存关浏览器)
     就得从零重学,每次都是一次赌博。

改法:
  A. poll(): 只要学到【任一】接口就主动重放;完全没学到才等学习窗口。
     回退 CDP 之后仍每 3 轮试一次主动重放(重放比被动捕获确定得多)。
  B. _enable_cdp_fallback(): 装监听的同时后台 reload 一次直播页,逼 SPA 重新发请求。
  C. 新增 data/live_api_spec.json 持久化规格(按账号),重启/重建后首轮 poll 即可出数。
  D. 检测期改两轮:20s 无信号 -> relearn(重载+重开学习窗口) -> 再 12s。
  E. 判定失败的日志补 url / learned / cdp_fallback,下次出事能直接看出来是"被弹回
     /platform"还是"接口没触发"。

不改:接口字段解析、is_live 判定规则(live_object_id 仍不单独作为直播信号)、内存精简。
"""
import io

LF = r"D:\SPHGJ\backend\live_fetcher.py"
AM = r"D:\SPHGJ\backend\account_manager.py"

PATCHES_LF = [
    # ---- 1. import os + 规格缓存 helper ----
    (
        """import asyncio
import json
import re
import time
import logging
from collections import deque
from datetime import datetime
from .selectors import LIVE_URL
from .metrics import extract_all, shrink_conv
""",
        """import asyncio
import json
import os
import re
import time
import logging
from collections import deque
from datetime import datetime
from .selectors import LIVE_URL
from .metrics import extract_all, shrink_conv

# ======================== rev43: 已学 API 规格持久化 ========================
# 现象:学到的 get_live_info/check_live_status 规格只活在内存。live_page 一重建
# (定期释放 renderer / 省内存关浏览器后重开)就得从零重学,而学习依赖页面自发请求,
# 时机过了就学不到 -> 误判"未在直播"。落到磁盘后重建即继承,首轮 poll 就能出数。
_SPEC_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "live_api_spec.json")


def _load_spec_cache(account_id):
    \"\"\"读该账号已学到的 API 规格(文件不存在/损坏一律返回 {})。\"\"\"
    try:
        with io_open(_SPEC_CACHE_PATH) as f:
            data = json.load(f)
        spec = data.get(account_id) or {}
        # 只认结构完整的条目,防止半截文件把轮询带进死循环
        return {k: v for k, v in spec.items()
                if isinstance(v, dict) and v.get("url")}
    except Exception:
        return {}


def _save_spec_cache(account_id, learned):
    \"\"\"写回该账号的 API 规格(tmp -> os.replace 原子写;失败静默,不影响抓取)。\"\"\"
    try:
        os.makedirs(os.path.dirname(_SPEC_CACHE_PATH), exist_ok=True)
        try:
            with io_open(_SPEC_CACHE_PATH) as f:
                data = json.load(f)
        except Exception:
            data = {}
        clean = {}
        for k, v in (learned or {}).items():
            if not isinstance(v, dict) or not v.get("url"):
                continue
            headers = {hk: hv for hk, hv in (v.get("headers") or {}).items()
                       if hk.lower() != "cookie"}   # cookie 绝不落盘
            clean[k] = {"url": v["url"], "method": v.get("method", "POST"),
                        "headers": headers, "postData": v.get("postData")}
        if not clean:
            return
        data[account_id] = clean
        tmp = _SPEC_CACHE_PATH + ".tmp"
        with io_open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _SPEC_CACHE_PATH)
    except Exception:
        pass


def io_open(path, mode="r"):
    \"\"\"小工具:统一 utf-8 打开(读写都走这里,避免各处编码不一致)。\"\"\"
    return open(path, mode, encoding="utf-8")
""",
        "imports+cache",
    ),
    # ---- 2. __init__: 无继承时读磁盘缓存 + 新增两个状态位 ----
    (
        """        self._learned = dict(learned) if learned else {}   # {"live_info": spec, "live_status": spec}
        self._inherited = bool(learned)                    # 规格是否来自继承(用于日志区分)
        self._learning = not self._learned                 # 已继承则无需再学
        self._learn_deadline = 0.0      # 学习超时阈值(>此时间未学到则回退 CDP)
        self._poll_fail_streak = 0      # 主动轮询连续失败计数
        self._cdp_fallback = False      # 主动轮询不可用回退到 CDP 响应捕获
""",
        """        self._learned = dict(learned) if learned else {}   # {"live_info": spec, "live_status": spec}
        # rev43: 内存没继承到 -> 读磁盘缓存(data/live_api_spec.json),避免每次重建都从零学
        if not self._learned:
            cached = _load_spec_cache(account_id)
            if cached:
                self._learned = cached
                logger.info(f"[live:{account_id}] 从磁盘缓存载入已学 API 规格: {sorted(cached)}")
        self._inherited = bool(self._learned)              # 规格是否来自继承/缓存(用于日志区分)
        self._learning = not self._learned                 # 已继承则无需再学
        self._learn_deadline = 0.0      # 学习超时阈值(>此时间未学到则回退 CDP)
        self._poll_fail_streak = 0      # 主动轮询连续失败计数
        self._cdp_fallback = False      # 主动轮询不可用回退到 CDP 响应捕获
        self._replay_tick = 0           # rev43: 回退 CDP 后的主动重放节流计数
        self._reload_nudged = False     # rev43: 学习超时只自动 reload 一次,防抖
""",
        "init",
    ),
    # ---- 3. _on_req_learn: 学到任一即剔除 cookie + 落盘 ----
    (
        """            if "get_live_info" in url:
                self._learned["live_info"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_info API: {url[:90]}")
            elif "check_live_status" in url:
                self._learned["live_status"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_status API: {url[:90]}")
""",
        """            learned_before = len(self._learned)
            if "get_live_info" in url:
                self._learned["live_info"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_info API: {url[:90]}")
            elif "check_live_status" in url:
                self._learned["live_status"] = self._capture_req(req)
                logger.debug(f"[live:{self.account_id}] 学到 live_status API: {url[:90]}")
            if len(self._learned) > learned_before:
                # rev43: 学到【一个】就立即剔除 cookie 并落盘。原实现要等两个都学到才处理,
                # 于是"只学到 live_status"的实例既用不上也不留档,下次重建还得重学。
                self._strip_cookie_headers()
                _save_spec_cache(self.account_id, self._learned)
""",
        "learn-save",
    ),
    # ---- 4. poll(): 有任一 spec 就主动重放弱化"全有或全无" ----
    (
        """        # 学习未完成:等学到;超时则回退 CDP
        if self._learning and not self._cdp_fallback:
            if time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
        # 已回退 CDP: 数据由 responseReceived 被动填充,这里仅返回快照
        if self._cdp_fallback:
            return await self.snapshot()
""",
        """        # rev43: 只要学到【任一】接口就主动重放,不再要求两个都学到。
        # 原逻辑 _learning 是"全有或全无"开关:15s 内只学到 live_status(缺 live_info)
        # 就完全不主动轮询,直接掉进 CDP 被动捕获 —— 而此刻页面早已把 check_live_status
        # 发完,被动监听永远等不到新响应 -> 误判"未在直播"(2026-10-28 2 号事故)。
        if not self._learned:
            # 一个都没学到:等学习窗口;超时则回退 CDP(回退时自动 reload 重触发请求)
            if self._learning and not self._cdp_fallback and time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
        # 已回退 CDP:以被动捕获为主,但仍每 3 轮主动重放一次
        # (直播间不刷新时,页面可能长时间不再发 check_live_status,纯被动会饿死)
        if self._cdp_fallback:
            self._replay_tick = (self._replay_tick + 1) % 3
            if self._replay_tick != 0:
                return await self.snapshot()
""",
        "poll-gate",
    ),
    # ---- 5. _enable_cdp_fallback: 装监听 + 后台 reload 一次 ----
    (
        """        try:
            if self._cdp is not None:
                self._cdp.on("Network.responseReceived", self._on_cdp_response)
                self._cdp_fallback = True
                logger.warning(f"[live:{self.account_id}] 直播检测回退到 CDP 响应捕获(主动轮询不可用)")
        except Exception as e:
""",
        """        try:
            if self._cdp is not None:
                self._cdp.on("Network.responseReceived", self._on_cdp_response)
                self._cdp_fallback = True
                logger.warning(f"[live:{self.account_id}] 直播检测回退到 CDP 响应捕获(主动轮询不可用)")
                # rev43: 被动监听只能看到【之后】发出的响应,而 check_live_status 通常在
                # 页面初始化时就发完了。不 reload 的话这段监听就是空等,必然误判下播。
                if not self._learned:
                    self._schedule_reload("学习超时")
        except Exception as e:
""",
        "fallback-reload",
    ),
    # ---- 6. 新增 _schedule_reload / relearn ----
    (
        """    async def _fetch_replay(self, spec):
""",
        """    def _schedule_reload(self, reason: str):
        \"\"\"rev43: 后台 reload 一次直播页,逼 SPA 重新发出 check_live_status/get_live_info。

        只做一次(同一实例不重复),任务进 _cdp_tasks 追踪,close() 时会被 cancel。
        这是 CDP 回退能起作用的【前提】:被动捕获看不到已经发生过的请求。
        \"\"\"
        if self._reload_nudged or self.page is None:
            return
        self._reload_nudged = True

        async def _do():
            try:
                if self.page is None:
                    return
                await _with_timeout(self.page.reload(wait_until="domcontentloaded"), 25)
                logger.info(f"[live:{self.account_id}] 已重载直播页({reason}),等页面重新发出检测请求")
            except Exception as e:
                logger.debug(f"[live:{self.account_id}] 重载直播页失败: {e}")

        try:
            t = asyncio.create_task(_do())
            self._cdp_tasks.add(t)
            t.add_done_callback(self._cdp_tasks.discard)
        except Exception:
            pass

    async def relearn(self):
        \"\"\"rev43: 检测阶段第二轮用 —— 重载直播页,并(在仍无规格时)重开学习窗口。

        返回是否真的重载了(页面已关/重载失败返回 False)。
        \"\"\"
        if self.page is None:
            return False
        self.updated_at_ts = 0.0
        r = await _with_timeout(self.page.reload(wait_until="domcontentloaded"), 25)
        if r is None and getattr(self.page, "is_closed", lambda: False)():
            return False
        if not self._learned and self._cdp is not None:
            # 仍无规格:重新 arm 学习监听(10s 窗口),页面重载后请求会重新发出
            try:
                self._learning = True
                self._learn_deadline = time.time() + 10
                self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
                logger.info(f"[live:{self.account_id}] 重载后重新开始学习直播检测 API(10s 窗口)")
            except Exception:
                pass
        # 已回退 CDP 的实例:reload 后被动捕获自然重新工作,清一下节流计数
        self._replay_tick = 0
        return True

    async def _fetch_replay(self, spec):
""",
        "reload-helpers",
    ),
    # ---- 7. snapshot 带 page_url ----
    (
        """        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
            "metrics": self._dashboard_cache or {},
        }
""",
        """        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
            "metrics": self._dashboard_cache or {},
            # rev43: 带上当前页面 URL —— 被判"未在直播"时能一眼看出是不是被弹回了 /platform
            "page_url": (self.page.url if self.page is not None else ""),
        }
""",
        "snapshot-url",
    ),
]

PATCHES_AM = [
    # ---- 8. 两轮检测 ----
    (
        """        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            # rev13: 主动轮询(替代被动等响应)——页面加载后由 fetcher 主动拉取直播数据
            await lf.poll()
            info = await lf.snapshot()
            if info.get("live_stats") or lf.live_object_id or lf.stream_url:
                return info
            await asyncio.sleep(2)
        return info or {}
""",
        """        async def _round(secs):
            deadline = time.time() + secs
            info = {}
            while time.time() < deadline:
                # rev13: 主动轮询(替代被动等响应)——页面加载后由 fetcher 主动拉取直播数据
                await lf.poll()
                info = await lf.snapshot()
                if info.get("live_stats") or lf.live_object_id or lf.stream_url:
                    return info
                await asyncio.sleep(2)
            return info or {}

        info = await _round(timeout)
        if info.get("live_stats") or lf.live_object_id or lf.stream_url:
            return info
        # rev43: 首轮没信号【不再直接判下播】。学习超时点在第一轮尾声,剩下的几秒只够
        # CDP 被动监听空等(页面早已把 check_live_status 发完)-> 必然误判。重载一次直播页
        # 让请求重新发出,再给一轮时间。
        try:
            if await lf.relearn():
                logger.info(f"[{self.account['id']}] 首轮未取到直播信号,重载直播页重试一轮")
                info = await _round(12.0)
        except Exception as e:
            logger.debug(f"[{self.account['id']}] 重载重试失败: {e}")
        return info or {}
""",
        "two-round",
    ),
    # ---- 9. 未在直播日志补诊断字段 ----
    (
        """            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(live_stats={info.get('live_stats') is not None}, "
                        f"live_object_id={lf.live_object_id})")
""",
        """            logger.info(f"[{self.account['id']}] 直播检测:当前未在直播"
                        f"(live_stats={info.get('live_stats') is not None}, "
                        f"live_object_id={lf.live_object_id}, "
                        f"learned={sorted(getattr(lf, '_learned', {}) or {})}, "
                        f"cdp_fallback={getattr(lf, '_cdp_fallback', None)}, "
                        f"url={(info.get('page_url') or '')[-40:]})")
""",
        "log-detect",
    ),
    # ---- 10. 信号中断日志:last_ts=0 时别打 1.79e9 秒 ----
    (
        """                            logger.info(f"[{self.account['id']}] 直播信号中断"
                                        f"(距今{int(now - last_ts)}s),原地重试 poll "
""",
        """                            _gap = "从未取到信号" if not last_ts else f"距今{int(now - last_ts)}s"
                            logger.info(f"[{self.account['id']}] 直播信号中断"
                                        f"({_gap}),原地重试 poll "
""",
        "log-gap",
    ),
]

for path, patches, name in ((LF, PATCHES_LF, "live_fetcher.py"),
                            (AM, PATCHES_AM, "account_manager.py")):
    s = io.open(path, encoding="utf-8").read()
    for old, new, tag in patches:
        n = s.count(old)
        assert n == 1, f"[{name}/{tag}] 命中 {n} 次,应为 1 次"
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)
    print(f"PATCH OK {name}: {len(patches)} 处")

# ---------------- 自校验 ----------------
lf = io.open(LF, encoding="utf-8").read()
am = io.open(AM, encoding="utf-8").read()
checks = [
    ("import os", "import os\n" in lf),
    ("spec cache helper", "_load_spec_cache" in lf and "_save_spec_cache" in lf),
    ("cache load in init", "_load_spec_cache(account_id)" in lf),
    ("learn 即落盘", "_save_spec_cache(self.account_id, self._learned)" in lf),
    ("poll 任一 spec 即可用", "if not self._learned:" in lf),
    ("回退后 3 轮重放", "_replay_tick" in lf),
    ("回退时 reload", 'self._schedule_reload("学习超时")' in lf),
    ("relearn 方法", "async def relearn(self)" in lf),
    ("snapshot 带 url", '"page_url"' in lf),
    ("两轮检测", "await lf.relearn()" in am),
    ("未直播日志带诊断", "cdp_fallback={getattr(lf, '_cdp_fallback', None)}" in am),
    ("信号中断日志修正", "_gap = \"从未取到信号\"" in am),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + " " + n)
assert not bad, "自校验失败: " + ",".join(bad)
print("SELFCHECK PASS")
