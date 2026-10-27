# -*- coding: utf-8 -*-
"""rev47 patch 1/2: live_fetcher 增加「统一判据 + DOM 文本兜底」

用户在 2026-10-28 定的方向:
  ① 判定口径统一(现在检测阶段 / 等待循环 / 直播循环三处各写一遍,已经出过两次不一致 bug)
  ② 先用 DOM 文本兜底(接口层 check_live_status 未开播时没有任何"在播"布尔位)

本补丁只改 live_fetcher.py:
  - 新增模块级 judge_live() —— 全项目唯一的"是否在播"判据
  - 新增 DOM 关键词表 + read_dom_state_from_text()
  - LiveFetcher 增加 status_num / dom_state / dom_ts 字段
  - 新增 read_dom_state():在直播页(含 iframe)读 innerText 推断状态,首次落盘诊断
  - snapshot() 把上述字段暴露出去,供 account_manager 判定与前端展示
"""
import io

P = r"D:\SPHGJ\backend\live_fetcher.py"

PATCHES = []

# ---------------------------------------------------------------- 1. 判据 + DOM 词表
PATCHES.append((
    '_SLIM_FLV_URLS = ["*.flv*", "*.m3u8*", "*wxlivecdn*", "*trtc*"]\n\n\nclass LiveFetcher:',
    '''_SLIM_FLV_URLS = ["*.flv*", "*.m3u8*", "*wxlivecdn*", "*trtc*"]


# ======================== rev47: 全项目唯一的直播判据 ========================
# 为什么要统一:rev43~46 期间"判定口径不一致"造成了三类连环误判 ——
#   ① _wait_for_live_detection 拿 live_object_id 就提前返回,与判定(只认 live_stats)不一致
#   ② _open_and_detect_live 把 stream_url 当在播信号,而 _live_loop 只认 live_stats
#      -> 检测说"在播"、循环说"信号中断",白跑 3 次重试 + reload + 12s
#   ③ 两处各自维护 fresh 窗口(30s / 90s),改一处忘另一处
# 从 rev47 起:任何地方判"是否在播"都必须调 judge_live(),不要再就地写表达式。

# DOM 关键词(保守集合)。2026-10-28 三个账号 check_live_status 完整响应 dump 确认:
# 未开播时只有 status=0,没有 isLive/isLiving 这类布尔位,也没有播放地址;
# 所以页面文本是最廉价、最不依赖接口字段的兜底判据。
# 词表刻意保守:"拿不准一律 None",绝不用它下"未播"的结论(只作弱正向)。
_DOM_NOT_LIVE_WORDS = ("未开播", "暂未开播", "当前未开播", "直播已结束", "已结束直播", "暂无直播")
_DOM_LIVE_WORDS = ("正在直播", "直播中", "结束直播", "直播时长", "在线人数")


def read_dom_state_from_text(txt):
    """从直播大屏页文本推断状态。返回 (True=看起来在播 / False=看起来未播 / None=判断不了, 命中词)。"""
    if not txt:
        return None, ""
    for w in _DOM_NOT_LIVE_WORDS:
        if w in txt:
            return False, w
    for w in _DOM_LIVE_WORDS:
        if w in txt:
            return True, w
    return None, ""


def judge_live(live_stats=None, stats_fresh=True, stream_url=None,
               allow_stream_url=True, dom_state=None, dom_fresh=True,
               status=None, log_account=""):
    """统一直播判据。返回 (is_live: bool, reason: str)。

    优先级(按实测可靠度从高到低):
      1. live_stats 新鲜        —— 最强,get_live_info / check_live_status 带 liveStats 才有
      2. DOM 明确"在播"          —— rev47 兜底,不依赖接口字段
      3. DOM 明确"未开播"        —— 同上,负向
      4. stream_url(.flv/.m3u8) —— 检测阶段可用;长驻页面不要用,它是 sticky 的
                                   (拿到一次就再也不会被清空,用它判活会永远判在播)
      5. status == 0            —— check_live_status.status,0=未开播(三账号 dump 实证)

    allow_stream_url=False 供 _live_loop 这类"长驻页面判信号是否还活着"的场景使用。
    """
    if live_stats and stats_fresh:
        return True, "live_stats"
    if dom_state is True and dom_fresh:
        return True, "dom:在播"
    if dom_state is False and dom_fresh:
        return False, "dom:未开播"
    if allow_stream_url and stream_url:
        return True, "stream_url"
    if status == 0:
        return False, "status=0"
    return False, "no_signal"


class LiveFetcher:''',
    "judge",

))

# ---------------------------------------------------------------- 2. 新增字段
PATCHES.append((
    '        self.live_flag = None           # rev46: check_live_status 里的布尔直播标志(True/False/未知)',
    '''        self.live_flag = None           # rev46: check_live_status 里的布尔直播标志(True/False/未知)
        # rev47: 统一判据所需的两个事实来源
        self.status_num = None          # check_live_status.status(0=未开播,实测确认)
        self.dom_state = None           # 直播大屏页文本推断(True/False/None)
        self.dom_ts = 0.0               # dom_state 的采样时间
        self._dom_dumped = False        # 页面文本只落盘一次(诊断用)''',
    "init",
))

# ---------------------------------------------------------------- 3. 记录 status
PATCHES.append((
    '        live_id = d.get("liveObjectId") or self._find_live_object_id(d)',
    '''        # rev47: status 纳入统一判据(0=未开播,经三账号完整响应 dump 实证)
        if d.get("status") is not None:
            self.status_num = d.get("status")
        live_id = d.get("liveObjectId") or self._find_live_object_id(d)''',
    "status",
))

# ---------------------------------------------------------------- 4. read_dom_state 方法
PATCHES.append((
    '    # ======================== rev11 回退: CDP 响应捕获(仅回退模式启用) ========================',
    '''    # ======================== rev47: 直播页文本判据 ========================

    async def read_dom_state(self, dump=True):
        """读直播大屏页(含 iframe)的可视文本,推断"是否在播"。返回 (state, matched)。

        为什么要 iframe:直播控制台主体在 micro/live/liveBuild 里,wujie 微前端
        常把内容渲染进 iframe,主文档 innerText 可能是空的(与 rev30 二维码同源问题)。
        因此逐 frame 取样再拼起来,最多 4 个 frame、每个 2000 字,开销可控。

        dump=True 时首次把全文落盘 data/live_probe/<账号>_dom.txt —— 关键词表目前
        是"保守猜测"(现网未开播时的页面文案还没核对过),先观测一轮再收紧。
        """
        texts = []
        try:
            frames = list(self.page.frames)[:4] if self.page is not None else []
        except Exception:
            frames = []
        js = ("() => { const b = document.body; if (!b) return '';"
              " return (b.innerText || '').replace(/\\\\s+/g, ' ').slice(0, 2000); }")
        for fr in frames:
            try:
                r = await _with_timeout(fr.evaluate(js), 8)
                if r:
                    texts.append(r)
            except Exception:
                pass
        txt = " || ".join(texts)
        state, matched = read_dom_state_from_text(txt)
        self.dom_state = state
        self.dom_ts = time.time()
        if dump and not self._dom_dumped and txt:
            self._dom_dumped = True
            try:
                _d = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_d, exist_ok=True)
                with io_open(os.path.join(_d, f"{self.account_id}_dom.txt"), "w") as f:
                    f.write(txt)
            except Exception:
                pass
            logger.info(f"[live:{self.account_id}] 直播页文本采样({len(txt)}字,"
                        f"frames={len(frames)}): 命中={matched or '无'} "
                        f"状态={'在播' if state is True else ('未播' if state is False else '未知')} "
                        f"| {txt[:180]}")
        return state, matched

    # ======================== rev11 回退: CDP 响应捕获(仅回退模式启用) ========================''',
    "dom-method",
))

# ---------------------------------------------------------------- 5. snapshot 暴露
PATCHES.append((
    '''            "live_flag": self.live_flag,   # rev46: check_live_status 的布尔直播标志''',
    '''            "live_flag": self.live_flag,   # rev46: check_live_status 的布尔直播标志
            # rev47: 统一判据的输入项
            "status_num": self.status_num,
            "dom_state": self.dom_state,
            "dom_ts": self.dom_ts,''',
    "snapshot",
))

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------------------------------------------------------- 自校验
s = io.open(P, encoding="utf-8").read()
checks = [
    ("judge_live 定义", "def judge_live(" in s),
    ("DOM 词表", "_DOM_NOT_LIVE_WORDS" in s and "_DOM_LIVE_WORDS" in s),
    ("read_dom_state_from_text", "def read_dom_state_from_text(" in s),
    ("status_num 字段", "self.status_num = None" in s),
    ("dom_state 字段", "self.dom_state = None" in s),
    ("read_dom_state 方法", "async def read_dom_state(self" in s),
    ("snapshot 带 status_num", '"status_num": self.status_num,' in s),
    ("snapshot 带 dom_state", '"dom_state": self.dom_state,' in s),
    ("类定义仍在", "class LiveFetcher:" in s),
    ("判据只有一份定义", s.count("def judge_live(") == 1),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK   " if ok else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS  live_fetcher.py")
