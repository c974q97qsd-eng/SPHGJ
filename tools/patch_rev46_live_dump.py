# -*- coding: utf-8 -*-
"""rev46: 直播检测(四) —— 把 check_live_status 完整响应落盘,定位"是否在播"的判据字段

rev45 的 700 字 dump 只能看到一半,已确认的数据结构:
    {"errCode":0,"errMsg":"request successful","data":{
        "coverImgList":[], "status":0, "liveObjectId":"1504...", "liveId":"2042...",
        "wxNickname":"...", "switchStatus":{...}, "rewardAvailable":0/1,
        "operatorRole":2, "liveLogoAvailable":0, ...(被截断,4 号此处疑似 isLive/isLiving)
    }}

现网没有 get_live_info,所以 is_live 的判据必须落在 check_live_status 上。
本版:
  A. 首次解析时把【完整】响应体写到 data/live_probe/<account_id>.json(覆盖式,不堆积),
     离线就能看清全部字段;
  B. 日志只打关键字段摘要(status / isLive / liveStatus / liveObjectId / liveId /
     coverImgList 条数 / data 顶层 key 列表),一行看完,不刷屏;
  C. 若响应里存在明确的布尔直播标志(isLive / isLiving / isLivingNow),直接当作
     live_stats 的替代判据(置一个 _live_flag),供上层判定使用。
"""
import io

LF = r"D:\SPHGJ\backend\live_fetcher.py"

PATCHES = [
    # ---- 1. dump 改成写文件 + 摘要日志 ----
    (
        """        # rev45: 首次 dump 原始响应,确认 check_live_status 到底返回哪些字段
        if not self._status_body_dumped:
            self._status_body_dumped = True
            logger.info(f"[live:{self.account_id}] check_live_status 原始响应"
                        f"(前 700 字): {txt[:700]}")
""",
        """        # rev46: 首次把【完整】响应落盘 + 打关键字段摘要(现网没有 get_live_info,
        # 是否在播的判据只能从这里的字段里找)
        if not self._status_body_dumped:
            self._status_body_dumped = True
            try:
                _dump_dir = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_dump_dir, exist_ok=True)
                with io_open(os.path.join(_dump_dir, f"{self.account_id}.json"), "w") as f:
                    f.write(txt)
            except Exception:
                pass
            flag_vals = {k: d.get(k) for k in
                         ("status", "isLive", "isLiving", "isLivingNow", "liveStatus")}
            logger.info(f"[live:{self.account_id}] check_live_status 摘要: {flag_vals}, "
                        f"liveObjectId={d.get('liveObjectId')}, liveId={d.get('liveId')}, "
                        f"coverImg={len(d.get('coverImgList') or [])}项, "
                        f"data_keys={sorted(d.keys())}")
""",
        "dump-file",
    ),
    # ---- 2. 明确的布尔直播标志 -> 作为 live_stats 替代判据 ----
    (
        """        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
        # 若响应里自带 liveStats(直接字段或嵌套在 liveInfo 下),直接采用。
        stats = d.get("liveStats") or (d.get("liveInfo") or {}).get("liveStats")
        if isinstance(stats, dict) and stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
            logger.info(f"[live:{self.account_id}] 从 check_live_status 取到 liveStats"
                        f"(字段数={len(stats)}),is_live 判定可用")
""",
        """        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
        # 若响应里自带 liveStats(直接字段或嵌套在 liveInfo 下),直接采用。
        stats = d.get("liveStats") or (d.get("liveInfo") or {}).get("liveStats")
        if isinstance(stats, dict) and stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
            logger.info(f"[live:{self.account_id}] 从 check_live_status 取到 liveStats"
                        f"(字段数={len(stats)}),is_live 判定可用")
        # rev46: 响应里若有明确的布尔直播标志,记为判据(供上层在无 liveStats 时使用)
        for _k in ("isLive", "isLiving", "isLivingNow"):
            _v = d.get(_k)
            if isinstance(_v, bool):
                if self.live_flag is not _v:
                    logger.info(f"[live:{self.account_id}] check_live_status.{_k}={_v}")
                self.live_flag = _v
                self.updated_at = datetime.now().isoformat()
                self.updated_at_ts = time.time()
                break
""",
        "bool-flag",
    ),
    # ---- 3. live_flag 初始值 + snapshot 暴露 ----
    (
        """        self._status_body_dumped = False  # rev45: check_live_status 原始响应只 dump 一次
""",
        """        self._status_body_dumped = False  # rev45: check_live_status 原始响应只 dump 一次
        self.live_flag = None           # rev46: check_live_status 里的布尔直播标志(True/False/未知)
""",
        "flag-init",
    ),
    (
        """            # rev43: 带上当前页面 URL —— 被判"未在直播"时能一眼看出是不是被弹回了 /platform
            "page_url": (self.page.url if self.page is not None else ""),
""",
        """            # rev43: 带上当前页面 URL —— 被判"未在直播"时能一眼看出是不是被弹回了 /platform
            "page_url": (self.page.url if self.page is not None else ""),
            "live_flag": self.live_flag,   # rev46: check_live_status 的布尔直播标志
""",
        "snapshot-flag",
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
    ("完整响应落盘", "live_probe" in s),
    ("摘要日志", "check_live_status 摘要" in s),
    ("布尔标志", "self.live_flag = _v" in s),
    ("snapshot 暴露 live_flag", '"live_flag": self.live_flag' in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + " " + n)
assert not bad, "自校验失败: " + ",".join(bad)
print("SELFCHECK PASS")
