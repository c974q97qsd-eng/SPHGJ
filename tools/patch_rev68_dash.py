# -*- coding: utf-8 -*-
"""rev68：大屏页被微信限制（「暂时无法使用该功能」）时的自激循环修复。

实测（母本 pid 28588，2026-11-07）：
  - 日志尾部 4000 行里 `live_page 定期 close+重建` 83 次、`打开直播大屏页取流` 13 次、
    `已注入登录态` 24 次，而配置周期是 1800s —— 重建次数远超应有节拍。
  - 页面文本采样反复是：`暂时无法使用该功能 我知道了`（14 字，命中=无 状态=未知）。
  - 链条：大屏页撞限制 -> 拿不到 liveStats -> 判「信号丢失」-> 重试 poll -> reload ->
    关页 -> HTTP 轻量探测仍说「在播(stream_url)」-> 重新开页 -> 再次撞限制（自激）。

修法（只改判定与节流，不动推流）：
  1. live_fetcher 加 `dom_blocked`：页面文本命中「暂时无法使用」类文案即为受限，
     snapshot() 一并带出，方便前端/日志区分「没开播」和「大屏被限」。
  2. 受限 -> 记退避窗口 `dashboard_blocked_backoff_sec`（默认 900s）：
     期间不再 close+重建、不再 reload 大屏页 —— 反复重开只会更频繁撞限制。
     推流不受影响：stream_url / live_stats 仍由 check_live_status 主动轮询负责。
  3. 新开 live_page 时把 `_last_live_reload` 拨到当下：否则 _live_loop 首次进循环
     会因为计时器停在很久以前，立刻又重建一次（白搭约 15s 数据空窗）。
"""
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # tools/ 的上一层才是项目根
LF = os.path.join(ROOT, "backend", "live_fetcher.py")
AM = os.path.join(ROOT, "backend", "account_manager.py")

PATCHES = [
    # ---------- live_fetcher.py ----------
    (LF, "LF1 受限词表",
     '_DOM_NOT_LIVE_WORDS = ("未开播", "暂未开播", "当前未开播", "直播已结束", "已结束直播", "暂无直播")\n',
     '_DOM_NOT_LIVE_WORDS = ("未开播", "暂未开播", "当前未开播", "直播已结束", "已结束直播", "暂无直播")\n'
     '# rev68: 微信对大屏页的功能限制提示。它不是"未播"，而是 dashboard 取不到数，\n'
     '# 继续重开/重载只会更频繁触发限制（2026-11-07 实测：83 次重建里大半是这么来的）。\n'
     '_DOM_BLOCKED_WORDS = ("暂时无法使用该功能", "该功能暂不可用", "暂时无法使用")\n'),

    (LF, "LF2 dom_blocked 字段",
     "        self.dom_state = None           # 直播大屏页文本推断(True/False/None)\n",
     "        self.dom_state = None           # 直播大屏页文本推断(True/False/None)\n"
     "        self.dom_blocked = False        # 大屏页是否被微信限制(「暂时无法使用该功能」)\n"),

    (LF, "LF3 采样时判定",
     '        txt = " || ".join(texts)\n'
     '        state, matched = read_dom_state_from_text(txt)\n',
     '        txt = " || ".join(texts)\n'
     '        # rev68: 先认「被限制」再认在播/未播 —— 受限页同时含后台导航文案，\n'
     '        # 拿它当"未播"会让上层立刻关页重开，正是自激循环的起点。\n'
     '        self.dom_blocked = bool(txt) and any(w in txt for w in _DOM_BLOCKED_WORDS)\n'
     '        state, matched = read_dom_state_from_text(txt)\n'),

    (LF, "LF4 snapshot 带出",
     '            "dom_state": self.dom_state,\n'
     '            "dom_ts": self.dom_ts,\n',
     '            "dom_state": self.dom_state,\n'
     '            "dom_blocked": self.dom_blocked,\n'
     '            "dom_ts": self.dom_ts,\n'),

    # ---------- account_manager.py ----------
    (AM, "AM1 退避字段",
     "        self._live_closed_ts = 0.0   # live_page 关闭时间\n",
     "        self._live_closed_ts = 0.0   # live_page 关闭时间\n"
     "        self._dash_blocked_until = 0.0   # rev68: 大屏被微信限制的退避截止时间\n"),

    (AM, "AM2 新页拨计时器",
     "            self.live_page = await self.context.new_page()\n"
     "            # 用局部 lf 持有 LiveFetcher,避免并发 _close_context/release_idle_browser 将\n",
     "            self.live_page = await self.context.new_page()\n"
     "            # rev68: 新页就把重建计时器拨到当下。否则 _live_loop 首次进循环会因为\n"
     "            # _last_live_reload 还停在很久以前而立刻再重建一次（白搭约 15s 空窗）。\n"
     "            self._last_live_reload = time.time()\n"
     "            # 用局部 lf 持有 LiveFetcher,避免并发 _close_context/release_idle_browser 将\n"),

    (AM, "AM3 受限记退避",
     '            try:\n'
     '                await lf.read_dom_state(dump=True)\n'
     '                info = await lf.snapshot()\n'
     '            except Exception as e:\n'
     '                logger.debug(f"[{self.account[\'id\']}] 判定前读直播页文本失败: {e}")\n',
     '            try:\n'
     '                await lf.read_dom_state(dump=True)\n'
     '                info = await lf.snapshot()\n'
     '            except Exception as e:\n'
     '                logger.debug(f"[{self.account[\'id\']}] 判定前读直播页文本失败: {e}")\n'
     '            # rev68: 大屏页弹「暂时无法使用该功能」= 微信限制 dashboard，不是没信号。\n'
     '            # 记退避窗口，期间不再重建/重载大屏页；推流照旧（stream_url 由主动轮询给）。\n'
     '            if getattr(lf, "dom_blocked", False):\n'
     '                _bk = int(self.config.get("dashboard_blocked_backoff_sec", 900))\n'
     '                self._dash_blocked_until = time.time() + _bk\n'
     '                logger.warning(f"[{self.account[\'id\']}] 大屏页提示「暂时无法使用该功能」"\n'
     '                               f"(微信限制),dashboard 数据不可用;{_bk}s 内不再重建/重载大屏页")\n'),

    (AM, "AM4 重建受退避约束",
     '                if self.live_page and now - self._last_live_reload >= rebuild_sec:\n',
     '                if self.live_page and now - self._last_live_reload >= rebuild_sec \\\n'
     '                        and now >= getattr(self, "_dash_blocked_until", 0.0):\n'),

    (AM, "AM5 重载受退避约束",
     '                        if idle > 120 and can_reload:\n',
     '                        if idle > 120 and can_reload \\\n'
     '                                and now >= getattr(self, "_dash_blocked_until", 0.0):\n'),
]

ALREADY = "_dash_blocked_until" in io.open(AM, "rb").read().decode("utf-8")
if ALREADY:
    print("已打过 rev68 补丁，跳过替换，只跑校验")
else:
    # 同文件多处改动：全部在内存改完、全部 assert 通过，最后一次性写回。
    # （边改边写一旦中途失败会留下半改文件）
    buf = {}
    for path, tag, old, new in PATCHES:
        s = buf.get(path)
        if s is None:
            s = io.open(path, "rb").read().decode("utf-8")
        c = s.count(old)
        assert c == 1, "%s 锚点命中 %d 次（应为 1），未写盘" % (tag, c)
        buf[path] = s.replace(old, new, 1)
        print("OK  %-22s %s" % (tag, os.path.basename(path)))
    for path, s in buf.items():
        io.open(path, "wb").write(s.encode("utf-8"))
    print("WROTE %d 个文件" % len(buf))

# ---- 自校验 ----
lf = io.open(LF, "rb").read().decode("utf-8")
am = io.open(AM, "rb").read().decode("utf-8")
assert '_DOM_BLOCKED_WORDS = ("暂时无法使用该功能"' in lf
assert "self.dom_blocked = False" in lf
assert "self.dom_blocked = bool(txt) and any(" in lf
assert '"dom_blocked": self.dom_blocked,' in lf
assert "self._dash_blocked_until = 0.0" in am
assert "self._last_live_reload = time.time()" in am
assert 'self.config.get("dashboard_blocked_backoff_sec", 900)' in am
assert am.count("_dash_blocked_until") == 4, am.count("_dash_blocked_until")
print("SELFCHECK PASS  account_manager._dash_blocked_until 共 4 处引用")
