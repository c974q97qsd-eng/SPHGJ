# -*- coding: utf-8 -*-
"""rev44: 直播检测(二) —— 提前返回条件与判定条件对齐 + 补学 live_info

rev43 上线后的实测日志暴露了两个更深的问题:

1) 判定口径不一致(致命)
   _wait_for_live_detection 认为"拿到 live_object_id 就算有响应了" -> 首轮 2 秒就 return;
   而 _open_and_detect_live 的判定是 `live_stats and fresh`,live_object_id 明确【不参与】
   判定(注释写过:可能是上一场残留)。
   => 上一场残留的 liveObjectId 会让等待提前结束,等不到 get_live_info 返回的 liveStats
      -> 明明在播也判"未在直播"。

2) live_info 规格永远没学到
   日志里所有实例都是 learned=['live_status'],没有 live_info -> 主动轮询永远拿不到
   liveStats(它在 get_live_info 里),而 live_stats 又是 is_live 的唯一硬判据。
   继承到 live_status 后 _learning=False,连学习监听都不挂了,越跑越学不到。

改法:
  A. _wait_for_live_detection: 提前返回只看 live_stats / stream_url(与判定口径一致)。
  B. goto_live: 继承到 live_status 但缺 live_info 时,重开 15s 学习窗口专补 live_info;
     顺带采样含 "live" 的请求 URL(限 10 条),方便定位接口真名。
  C. poll: 学习窗口到期就摘监听(不留常驻事件 churn),已有规格照常轮询。
  D. _on_cdp_response: 域名判断 channels.weixin.qq.com -> weixin.qq.com
     (实测直播页在 ls.weixin.qq.com 域下,原判断会把整站请求全部丢弃)。
"""
import io

LF = r"D:\SPHGJ\backend\live_fetcher.py"
AM = r"D:\SPHGJ\backend\account_manager.py"

PATCHES_LF = [
    # ---- 1. __init__: 采样列表 ----
    (
        """        self._reload_nudged = False     # rev43: 学习超时只自动 reload 一次,防抖
""",
        """        self._reload_nudged = False     # rev43: 学习超时只自动 reload 一次,防抖
        self._urls_seen = []            # rev44: 学习窗口内采样到的 live 相关请求 URL(限 10 条)
""",
        "init-urls",
    ),
    # ---- 2. goto_live: 缺 live_info 时重开学习窗口 ----
    (
        """            else:
                # rev16 F4: 继承到规格,不注册学习监听 —— 零事件 churn,首轮 poll 即可出数
                logger.info(f"[live:{self.account_id}] 继承已学 API 规格(跳过学习监听),"
                            f"可直接主动轮询: {sorted(self._learned.keys())}")
""",
        """            elif "live_info" not in self._learned:
                # rev44: 继承到 live_status 却缺 live_info —— 这样拿不到 liveStats,
                # 而 liveStats 是 is_live 的唯一硬判据,等于永远判"未在直播"。
                # 重开一个 15s 学习窗口专补 live_info(poll 照常用已有规格主动轮询,不等它)。
                self._cdp.on("Network.requestWillBeSent", self._on_req_learn)
                self._learning = True
                self._learn_deadline = time.time() + 15
                logger.info(f"[live:{self.account_id}] 继承规格缺 live_info,"
                            f"重开 15s 学习窗口补学(已有: {sorted(self._learned)})")
            else:
                # rev16 F4: 继承到规格,不注册学习监听 —— 零事件 churn,首轮 poll 即可出数
                logger.info(f"[live:{self.account_id}] 继承已学 API 规格(跳过学习监听),"
                            f"可直接主动轮询: {sorted(self._learned.keys())}")
""",
        "goto-relearn-info",
    ),
    # ---- 3. _on_req_learn: 采样 live 相关 URL ----
    (
        """            req = (event.get("request") or {})
            url = req.get("url", "")
            learned_before = len(self._learned)
""",
        """            req = (event.get("request") or {})
            url = req.get("url", "")
            # rev44: 只在缺 live_info 时采样,帮助定位该接口的真实路径(限 10 条,不刷屏)
            if "live_info" not in self._learned and len(self._urls_seen) < 10 \\
                    and "live" in url.lower() and url not in self._urls_seen:
                self._urls_seen.append(url)
                logger.info(f"[live:{self.account_id}] live 请求采样"
                            f"({len(self._urls_seen)}/10): {url[:130]}")
            learned_before = len(self._learned)
""",
        "sample-urls",
    ),
    # ---- 4. poll: 学习窗口到期摘监听 ----
    (
        """        if not self._learned:
            # 一个都没学到:等学习窗口;超时则回退 CDP(回退时自动 reload 重触发请求)
            if self._learning and not self._cdp_fallback and time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
""",
        """        if not self._learned:
            # 一个都没学到:等学习窗口;超时则回退 CDP(回退时自动 reload 重触发请求)
            if self._learning and not self._cdp_fallback and time.time() > self._learn_deadline:
                await self._enable_cdp_fallback()
            return await self.snapshot()
        # rev44: 已有一份规格但学习窗口到期 -> 摘监听,别留常驻事件 churn
        if self._learning and time.time() > self._learn_deadline:
            self._end_learning("学习窗口结束")
""",
        "poll-end-learning",
    ),
    # ---- 5. _end_learning 方法 ----
    (
        """    def _schedule_reload(self, reason: str):
""",
        """    def _end_learning(self, reason: str):
        \"\"\"rev44: 结束学习窗口(摘监听),不影响已有规格的主动轮询。\"\"\"
        self._learning = False
        try:
            if self._cdp is not None:
                self._cdp.remove_listener("Network.requestWillBeSent", self._on_req_learn)
        except Exception:
            pass
        logger.info(f"[live:{self.account_id}] {reason},未学到 live_info;"
                    f"继续按已有规格轮询: {sorted(self._learned)}")

    def _schedule_reload(self, reason: str):
""",
        "end-learning",
    ),
    # ---- 6. CDP 回退域名放宽 ----
    (
        """            url = (event.get("response") or {}).get("url", "")
            if "channels.weixin.qq.com" not in url:
                return
""",
        """            url = (event.get("response") or {}).get("url", "")
            # rev44: 直播页实测跑在 ls.weixin.qq.com 域下,原判断只认 channels.weixin.qq.com
            # -> 该域下的 check_live_status/get_live_info 响应全被丢弃,回退等于没开。
            if "weixin.qq.com" not in url:
                return
""",
        "cdp-domain",
    ),
]

PATCHES_AM = [
    # ---- 7. 提前返回条件与判定口径对齐 ----
    (
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
""",
        """        # rev44: 提前返回条件必须与 _open_and_detect_live 的判定口径一致 ——
        # 判定只认 live_stats / stream_url,live_object_id 不参与(可能是上一场残留)。
        # 原实现拿 live_object_id 就提前 return,于是等不到 get_live_info 的 liveStats,
        # 明明在播也被判"未在直播"。
        def _hit(i):
            return bool(i.get("live_stats") or lf.stream_url)

        async def _round(secs):
            deadline = time.time() + secs
            info = {}
            while time.time() < deadline:
                # rev13: 主动轮询(替代被动等响应)——页面加载后由 fetcher 主动拉取直播数据
                await lf.poll()
                info = await lf.snapshot()
                if _hit(info):
                    return info
                await asyncio.sleep(2)
            return info or {}

        info = await _round(timeout)
        if _hit(info):
            return info
""",
        "wait-align",
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
    ("采样列表", "self._urls_seen = []" in lf),
    ("缺 live_info 重开学习", 'elif "live_info" not in self._learned:' in lf),
    ("URL 采样日志", "live 请求采样" in lf),
    ("学习窗口到期摘监听", "self._end_learning(\"学习窗口结束\")" in lf),
    ("_end_learning 定义", "def _end_learning(self, reason: str)" in lf),
    ("CDP 域名放宽", 'if "weixin.qq.com" not in url:' in lf),
    ("等待口径对齐", "def _hit(i):" in am),
    ("不再用 live_object_id 提前返回", "if info.get(\"live_stats\") or lf.live_object_id or lf.stream_url:" not in am),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + " " + n)
assert not bad, "自校验失败: " + ",".join(bad)
print("SELFCHECK PASS")
