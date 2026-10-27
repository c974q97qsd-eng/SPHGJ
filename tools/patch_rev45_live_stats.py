# -*- coding: utf-8 -*-
"""rev45: 直播检测(三) —— 现网没有 get_live_info,把 liveStats 的取数路径对齐真实接口

rev44 的 URL 采样已经把真相摆出来了(母本 01:33 实测,2/4 号一致):

    https://channels.weixin.qq.com/micro/live/liveBuild
    https://channels.weixin.qq.com/micro/live/cgi-bin/mmfinderassistant-bin/live/check_live_status
    (其余全是 helper_mmdata / notification_list / get_auth_info / 静态 js)

=> 页面上【从未发出 get_live_info 请求】。于是:
   - `live_info` 规格永远学不到(日志里 learned 只有 ['live_status'],已反复确认);
   - 而 is_live 的唯一硬判据是 live_stats,它只从 get_live_info 的 liveStats 取;
   - 结果:哪怕账号真在播,只要 check_live_status 不给 audiencePlayUrl,就必然判"未在直播"。

改法:
  A. _parse_live_status 里补 liveStats 取数路径 —— check_live_status 响应若带
     liveStats(或嵌套在 liveInfo 里)就直接用,不再依赖那条不存在的 get_live_info。
  B. 把 check_live_status 原始响应体 dump 一次(前 700 字),确认它到底给了哪些字段。
     拿到样本后再决定 liveStats 该怎么取/是否要换接口。
  C. 采样日志降噪:只记录含 cgi-bin 的 API 请求(过滤静态 js/页面 URL),限 5 条。
"""
import io

LF = r"D:\SPHGJ\backend\live_fetcher.py"

PATCHES = [
    # ---- 1. 采样降噪 ----
    (
        """            # rev44: 只在缺 live_info 时采样,帮助定位该接口的真实路径(限 10 条,不刷屏)
            if "live_info" not in self._learned and len(self._urls_seen) < 10 \\
                    and "live" in url.lower() and url not in self._urls_seen:
                self._urls_seen.append(url)
                logger.info(f"[live:{self.account_id}] live 请求采样"
                            f"({len(self._urls_seen)}/10): {url[:130]}")
""",
        """            # rev45: 只采样 cgi-bin 的 API 请求(静态 js/页面 URL 不是接口),限 5 条
            if "live_info" not in self._learned and len(self._urls_seen) < 5 \\
                    and "cgi-bin" in url and "live" in url.lower() \\
                    and url not in self._urls_seen:
                self._urls_seen.append(url)
                logger.info(f"[live:{self.account_id}] live API 采样"
                            f"({len(self._urls_seen)}/5): {url[:150]}")
""",
        "sample-tighten",
    ),
    # ---- 2. check_live_status 响应体 dump 一次 + liveStats 兜底取用 ----
    (
        """        d = data.get("data") or {}
        live_id = d.get("liveObjectId") or self._find_live_object_id(d)
""",
        """        d = data.get("data") or {}
        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
        # 若响应里自带 liveStats(直接字段或嵌套在 liveInfo 下),直接采用。
        stats = d.get("liveStats") or (d.get("liveInfo") or {}).get("liveStats")
        if isinstance(stats, dict) and stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
            logger.info(f"[live:{self.account_id}] 从 check_live_status 取到 liveStats"
                        f"(字段数={len(stats)}),is_live 判定可用")
        # rev45: 首次 dump 原始响应,确认 check_live_status 到底返回哪些字段
        if not self._status_body_dumped:
            self._status_body_dumped = True
            logger.info(f"[live:{self.account_id}] check_live_status 原始响应"
                        f"(前 700 字): {txt[:700]}")
        live_id = d.get("liveObjectId") or self._find_live_object_id(d)
""",
        "status-body",
    ),
    # ---- 3. dump 标志位 ----
    (
        """        self._urls_seen = []            # rev44: 学习窗口内采样到的 live 相关请求 URL(限 10 条)
""",
        """        self._urls_seen = []            # rev44: 学习窗口内采样到的 live 相关请求 URL(限 5 条)
        self._status_body_dumped = False  # rev45: check_live_status 原始响应只 dump 一次
""",
        "flag",
    ),
]

s = io.open(LF, encoding="utf-8").read()
for old, new, tag in PATCHES:
    n = s.count(old)
    assert n == 1, f"[{tag}] 命中 {n} 次,应为 1 次"
    s = s.replace(old, new, 1)
io.open(LF, "w", encoding="utf-8", newline="").write(s)
print(f"PATCH OK live_fetcher.py: {len(PATCHES)} 处")

s = io.open(LF, encoding="utf-8").read()
checks = [
    ("采样降噪", "live API 采样" in s),
    ("status body dump", "_status_body_dumped" in s),
    ("liveStats 兜底取用", 'stats = d.get("liveStats") or (d.get("liveInfo") or {}).get("liveStats")' in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + " " + n)
assert not bad, "自校验失败: " + ",".join(bad)
print("SELFCHECK PASS")
