# -*- coding: utf-8 -*-
"""rev69 单测:大屏纯 HTTP + 日志轮转。

本机没有 playwright(account_manager 直接 import 会失败),所以:
  - live_fetcher / log_rotate:真实导入,跑真实逻辑
  - account_manager:ast 抽出函数体 + fake self(与 test_rev66_view.py 同一套路)
"""
import ast
import asyncio
import builtins
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, extra))


def src(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def extract(rel, name, ns):
    """把函数定义单独抽出来 exec 到 ns,返回函数对象(去掉装饰器)。"""
    tree = ast.parse(src(rel))
    node = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
            break
    assert node is not None, "找不到 %s" % name
    node.decorator_list = []
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<%s>" % name, "exec"), ns)
    return ns[name]


# ============================================================
# A. LiveFetcher 纯 HTTP 路径(真实模块)
# ============================================================
from backend import live_fetcher as LFM          # noqa: E402

print("=== A. LiveFetcher 纯 HTTP ===")

ACC = {"id": "t1", "name": "测试号", "_aid": "AID1", "_log_finder_id": "FID1",
       "storage_state": {"cookies": [
           {"name": "sessionid", "value": "COOKIE-X",
            "domain": ".weixin.qq.com", "path": "/"}]}}
CFG = {"accounts": [ACC]}


async def _a1():
    lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)
    lf._learned = {"live_status": {"url": "u", "_tag": "status"}}
    called = []

    async def fake_http():
        called.append("http")
        return {}

    async def fake_replay(spec):
        called.append("replay")
        return ""

    lf._poll_http = fake_http
    lf._fetch_replay = fake_replay
    await lf.poll()
    check("A1 page=None -> poll 走纯 HTTP(不碰 _fetch_replay)",
          called == ["http"], called)


asyncio.run(_a1())


async def _a2():
    lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)
    lf._learned = {"live_status": {"url": "u", "_tag": "status"},
                   "live_info": {"url": "u2", "_tag": "info"}}
    lf.live_object_id = "OID-1"
    seen = []

    async def fake_post(spec):
        seen.append(spec.get("_tag"))
        return json.dumps({"errCode": 0, "data": {}})

    lf._http_post_spec = fake_post
    await lf._poll_http()
    check("A2 顺序 status -> info(liveObjectId 只能从 status 拿)", seen == ["status", "info"], seen)


asyncio.run(_a2())


async def _a3():
    lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)
    lf._learned = {"live_status": {"url": "u", "_tag": "status"},
                   "live_info": {"url": "u2", "_tag": "info"}}
    lf.live_object_id = None
    seen = []

    async def fake_post(spec):
        seen.append(spec.get("_tag"))
        return ""

    lf._http_post_spec = fake_post
    await lf._poll_http()
    check("A3 没有本场 liveObjectId 时跳过 info", seen == ["status"], seen)


asyncio.run(_a3())


async def _a4():
    lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)
    lf.live_object_id = "OID-NEW"
    cap = {}
    import backend.wx_http as WX

    orig = WX.post_json

    async def fake_post_json(account, url, body, page_url="", headers=None, timeout=0):
        cap["url"] = url
        cap["body"] = body
        return {"errCode": 0, "data": {"liveStats": {"currentOnlineCount": 8}}}

    WX.post_json = fake_post_json
    try:
        spec = {"url": "https://x/get_live_info?_aid=AID1", "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "postData": json.dumps({"liveObjectId": "OID-OLD",
                                        "timestamp": "1", "_log_finder_id": "FID1"})}
        txt = await lf._http_post_spec(spec)
    finally:
        WX.post_json = orig
    body = json.loads(cap.get("body") or "{}")
    check("A4 get_live_info 的 liveObjectId 换成本场",
          body.get("liveObjectId") == "OID-NEW", body.get("liveObjectId"))
    check("A5 旧场次 ID 不再出现",
          "OID-OLD" not in (cap.get("body") or ""), (cap.get("body") or "")[:120])
    check("A6 timestamp 被刷新(不是规格里那个 1)",
          body.get("timestamp") != "1", body.get("timestamp"))
    check("A7 成功时返回响应文本", bool(txt) and "liveStats" in txt, (txt or "")[:60])


asyncio.run(_a4())


async def _a8():
    lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)
    lf.live_object_id = None                       # 本场 ID 未知
    import backend.wx_http as WX
    orig = WX.post_json
    hit = []

    async def fake_post_json(*a, **k):
        hit.append(1)
        return {"errCode": 0, "data": {}}

    WX.post_json = fake_post_json
    try:
        spec = {"url": "https://x/get_live_info", "method": "POST",
                "headers": {}, "postData": json.dumps({"liveObjectId": "OLD"})}
        txt = await lf._http_post_spec(spec)
    finally:
        WX.post_json = orig
    check("A8 缺本场 ID 时不发请求(发了必然是空数据)", txt == "" and not hit, (txt, hit))


asyncio.run(_a8())


async def _a9():
    import backend.wx_http as WX
    orig = WX.post_json
    out = {}
    for tag, ret in (("errCode", {"errCode": 300333}), ("err", {"__err": "no_cookie"})):
        lf = LFM.LiveFetcher(None, "t1", account=ACC, config=CFG)

        async def fake_post_json(*a, **k):
            return ret

        WX.post_json = fake_post_json
        try:
            out[tag] = await lf._http_post_spec(
                {"url": "https://x/a", "method": "POST", "headers": {}, "postData": None})
        finally:
            WX.post_json = orig
        out[tag + "_why"] = lf._http_last_why
    check("A9 errCode!=0 -> 返回空并记录原因",
          out["errCode"] == "" and "300333" in out["errCode_why"], out)
    check("A10 __err -> 返回空并记录原因",
          out["err"] == "" and out["err_why"] == "no_cookie", out)


asyncio.run(_a9())


async def _a11():
    # account 里没有 _aid,只有 config 有 -> 必须能从 config 补上(纯 HTTP 没有页面读 localStorage)
    lf = LFM.LiveFetcher(None, "t1",
                         account={"id": "t1", "_aid": "", "_log_finder_id": ""},
                         config={"accounts": [{"id": "t1", "_aid": "AID-CFG",
                                               "_log_finder_id": "FID-CFG"}]})
    await lf._ensure_ids()
    check("A11 _ensure_ids 能只用 config 拿到 _aid/_log_finder_id",
          lf._aid == "AID-CFG" and lf._log_finder_id == "FID-CFG",
          (lf._aid, lf._log_finder_id))
    check("A12 _config_account 优先 config 里的最新对象",
          lf._config_account().get("_aid") == "AID-CFG", lf._config_account())


asyncio.run(_a11())


def _a13():
    lf = LFM.LiveFetcher(None, "t1", account={"id": "t1", "_aid": "OLD"},
                         config={"accounts": [{"id": "other", "_aid": "X"}]})
    check("A13 config 里没有本账号 -> 回退 self._account",
          lf._config_account().get("_aid") == "OLD", lf._config_account())


_a13()

# ============================================================
# B. account_manager 侧(ast 抽取 + fake self)
# ============================================================
print("\n=== B. account_manager ===")

AM_NS = {"logger": LFM.logger, "time": time, "LiveFetcher": LFM.LiveFetcher,
         "_load_spec_cache": None}
Attach = extract("backend/account_manager.py", "_attach_http_fetcher", AM_NS)
AfterProbe = extract("backend/account_manager.py", "_after_probe",
                     {**AM_NS, "logger": LFM.logger})


class FakeAM:
    def __init__(self, spec, cfg=None):
        self.account = {"id": "t1", "name": "测试号"}
        self.config = cfg or {}
        self.live_fetcher = None
        self.live_page = "PAGE-SENTINEL"
        self._live_opened_ts = 0.0
        self._live_closed_ts = 123.0
        self._last_probe = None
        self._probe_since_full = 0
        self.opened = []
        self.attach_ret = True

    async def _open_full(self, fast=False):
        self.opened.append(fast)
        return True


async def _b1():
    # 没有规格 -> 必须返回 False(否则纯 HTTP 拿不到任何数据,账号会永远"无信号")
    AM_NS["_load_spec_cache"] = lambda aid: {}
    fake = FakeAM({})
    ok = await Attach(fake)
    check("B1 无接口规格 -> 返回 False(交给开页去学)", ok is False, ok)

    # 只有一半规格也算没有
    AM_NS["_load_spec_cache"] = lambda aid: {"live_status": {"url": "u"}}
    ok = await Attach(fake)
    check("B2 规格不完整(status 有、info 缺) -> False", ok is False, ok)

    # 完整规格 -> 挂上,且绝不碰 live_page
    AM_NS["_load_spec_cache"] = lambda aid: {"live_status": {"url": "u"},
                                             "live_info": {"url": "u2"}}
    fake2 = FakeAM({})
    ok = await Attach(fake2)
    check("B3 规格完整 -> 挂上纯 HTTP fetcher", ok is True, ok)
    check("B4 fetcher.page 为 None(纯 HTTP)", fake2.live_fetcher is not None
          and fake2.live_fetcher.page is None, fake2.live_fetcher)
    check("B5 live_page 被清成 None(不开页)", fake2.live_page is None, fake2.live_page)
    check("B6 规格注入到 _learned",
          sorted(fake2.live_fetcher._learned) == ["live_info", "live_status"],
          fake2.live_fetcher._learned)
    check("B7 记下开启时间(供 idle 计算)", fake2._live_opened_ts > 0, fake2._live_opened_ts)


asyncio.run(_b1())


async def _b8():
    # _after_probe:默认纯 HTTP,不应调用 _open_full
    AM_NS["_load_spec_cache"] = lambda aid: {"live_status": {"url": "u"},
                                             "live_info": {"url": "u2"}}
    f = FakeAM({})
    AttachBound = Attach.__get__(f, FakeAM)
    f._attach_http_fetcher = lambda: AttachBound()
    probe = {"live": True, "reason": "stream_url", "via": "http", "status": 1}
    r = await AfterProbe(f, probe)
    check("B8 在播 + 默认配置 -> 挂纯 HTTP,不开页", r is True and f.opened == [], f.opened)

    # live_use_page=True -> 回到开页
    f2 = FakeAM({}, {"live_use_page": True})
    f2._attach_http_fetcher = lambda: asyncio.sleep(0, result=True)
    r2 = await AfterProbe(f2, probe)
    check("B9 live_use_page=True -> 仍然开页(开关可用)", r2 is True and f2.opened == [False],
          f2.opened)

    # 无规格(attach 返回 False) -> 回退开页学一次
    f3 = FakeAM({})
    f3._attach_http_fetcher = lambda: asyncio.sleep(0, result=False)
    r3 = await AfterProbe(f3, probe)
    check("B10 无规格 -> 回退开页学一次", r3 is True and f3.opened == [False], f3.opened)

    # 未播 -> 什么都不开
    f4 = FakeAM({})
    f4._attach_http_fetcher = lambda: asyncio.sleep(0, result=True)
    r4 = await AfterProbe(f4, {"live": False, "reason": "status=0", "via": "http", "status": 0})
    check("B11 未播 -> 不开页也不挂 fetcher", r4 is False and f4.opened == [], (r4, f4.opened))


asyncio.run(_b8())

AM_SRC = src("backend/account_manager.py")
LF_SRC = src("backend/live_fetcher.py")

check("B12 关页段对 live_page 判空(纯 HTTP 模式没有 page)",
      "if self.live_page is not None:   # rev69: 纯 HTTP 模式没有 page" in AM_SRC)
check("B13 DOM 采样只在有页面时做",
      "self.live_page is not None      # rev69: 纯 HTTP 实例没有页面可采样" in AM_SRC,
      "没找到 DOM 采样判空")
check("B14 纯 HTTP 长期无信号会回退开页自愈",
      "纯 HTTP 连续拿不到直播信号" in AM_SRC and "live_http_fallback_to_page" in AM_SRC)
check("B15 回退开页有 30 分钟限流(不反复开页)",
      "_http_open_fallback_ts\", 0) or 0) > 1800" in AM_SRC)
check("B16 4 处 LiveFetcher 构造都传了 config",
      AM_SRC.count("LiveFetcher(") == AM_SRC.count("config=self.config)"),
      (AM_SRC.count("LiveFetcher("), AM_SRC.count("config=self.config)")))

# ============================================================
# C. live_fetcher 别的落地点
# ============================================================
print("\n=== C. 其他落地断言 ===")
check("C1 poll 里 page=None 优先分流(在学习窗口判定之前)",
      "if self.page is None:\n            return await self._poll_http()\n        if not self._learned:"
      in LF_SRC, "顺序错了")
check("C2 _poll_http 只在纯 HTTP 实例生效,不影响页面实例",
      "if self.page is None:\n            return await self._poll_http()" in LF_SRC)
check("C3 _http_post_spec 复用 prepare_request(不另写一份解析)",
      "from .live_probe_http import prepare_request" in LF_SRC)
check("C4 构造函数收 config", "config=None):" in LF_SRC and "self._config = config or {}" in LF_SRC)

# ============================================================
# D. 日志轮转(真实模块)
# ============================================================
print("\n=== D. 日志轮转 ===")
from backend import log_rotate as LR              # noqa: E402

tmp = tempfile.mkdtemp(prefix="sphgj_log_")
try:
    lp = os.path.join(tmp, "run.log")
    ld = os.path.join(tmp, "logs")

    # A) 未超阈值且是今天 -> 继续追加
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("x" * 100)
    info, mode = LR.plan_startup(tmp, max_mb=20, keep_days=7)
    check("D1 小文件且是今天 -> 保持追加(a),不建归档",
          mode == "a" and info is None and os.path.getsize(lp) == 100, (mode, info))

    # B) 超过阈值 -> 只读复制归档 + 要求调用方用 w 打开
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("y" * (3 * 1048576))
    info, mode = LR.plan_startup(tmp, max_mb=2, keep_days=7)
    check("D2 超过阈值 -> 返回 w(调用方用 w 打开即截断)",
          bool(info) and mode == "w"
          and info.get("archived_from_mb", 0) >= 2.9, (mode, info))
    archived = (info or {}).get("archived_to") or ""
    check("D3 归档是只读复制,run.log 本身没被改名/删掉(避开 WinError 32)",
          os.path.exists(lp) and os.path.getsize(lp) == 3 * 1048576,
          os.path.getsize(lp) if os.path.exists(lp) else "missing")
    check("D4 归档文件名带时间戳",
          archived.startswith("run-") and archived.endswith(".log"), archived)
    check("D5 归档内容完整",
          os.path.getsize(os.path.join(ld, archived)) == 3 * 1048576,
          os.path.getsize(os.path.join(ld, archived)))

    # C) 完全按 main.py 的做法:用返回的模式打开文件
    fh = open(lp, mode, encoding="utf-8")
    try:
        check("D6 按 plan_startup 给的 w 打开 -> run.log 归零(这就是截断)",
              os.path.getsize(lp) == 0, os.path.getsize(lp))
        fh.write("after-truncate\n")
        fh.flush()
        check("D7 截断后写入正常(新日志从头开始)",
              io.open(lp, encoding="utf-8").read() == "after-truncate\n",
              io.open(lp, encoding="utf-8").read()[:40])
    finally:
        fh.close()

    # D) 复制成功但进程没截断就又重启 -> 复用同一份归档,不留重复文件
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("z" * (3 * 1048576))
    ia, ma = LR.plan_startup(tmp, max_mb=2, keep_days=7)
    n1 = len(os.listdir(ld))
    ib, mb_ = LR.plan_startup(tmp, max_mb=2, keep_days=7)
    n2 = len(os.listdir(ld))
    check("D8 同一份 run.log 重复归档 -> 复用已有文件(dedup),不新增第二份",
          bool(ia) and bool(ib) and ib.get("dedup") is True and n2 == n1,
          (ia, ib, n1, n2))
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("small")          # 收尾,后面的用例从干净状态开始

    # E) 跨天(小文件但 mtime 是几天前) -> 也要归档
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("q" * 50)
    old = time.time() - 3 * 86400
    os.utime(lp, (old, old))
    info3, mode3 = LR.plan_startup(tmp, max_mb=20, keep_days=7)
    check("D9 跨天(哪怕很小) -> 归档且要求 w",
          bool(info3) and info3.get("reason") == "跨天" and mode3 == "w",
          (mode3, info3))
    check("D10 归档时间取原文件 mtime",
          bool(info3) and info3["archived_to"].startswith(
              "run-" + time.strftime("%Y%m%d", time.localtime(old))),
          info3.get("archived_to") if info3 else None)
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("small")

    # F) keep_days 清理
    stale = os.path.join(ld, "run-20200101-000000.log")
    with io.open(stale, "w", encoding="utf-8") as f:
        f.write("old")
    os.utime(stale, (time.time() - 30 * 86400,) * 2)
    n = LR.cleanup(tmp, keep_days=7)
    check("D11 超过 keep_days 的归档被删", n >= 1 and not os.path.exists(stale), (n,))
    check("D12 近期归档保留",
          len([f for f in os.listdir(ld) if f.startswith("run-")]) >= 2,
          os.listdir(ld))

    # G) 运行中检查
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("w" * (5 * 1048576))
    check("D13 运行中 5MB / 阈值 2MB -> 需轮转",
          LR.should_rotate_running(tmp, 2) is True)
    check("D14 运行中 5MB / 阈值 20MB -> 不需轮转",
          LR.should_rotate_running(tmp, 20) is False)
    check("D15 size_mb 读数正确", abs(LR.size_mb(tmp) - 5.0) < 0.2, LR.size_mb(tmp))

    # H) 异常安全
    ok = True
    try:
        LR.plan_startup(os.path.join(tmp, "nope"), max_mb=1, keep_days=1)
        LR.cleanup(os.path.join(tmp, "nope"), 1)
        LR.should_rotate_running(os.path.join(tmp, "nope"), 1)
    except Exception as e:
        ok = False
        print("     ", e)
    check("D16 目录不存在也不抛异常(轮转失败不能影响启动)", ok)

    # I) 复制失败 -> 必须降级成 a,绝不把没归档的日志截掉
    with io.open(lp, "w", encoding="utf-8") as f:
        f.write("k" * (4 * 1048576))
    # mtime 挪到几小时前:确保归档名与上面几轮不同,不会命中 dedup 而跳过复制
    os.utime(lp, (time.time() - 12345.0,) * 2)
    before = os.path.getsize(lp)
    orig_copy = LR.shutil.copyfile
    LR.shutil.copyfile = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        info4, mode4 = LR.plan_startup(tmp, max_mb=2, keep_days=7)
        check("D17 复制失败 -> 降级为 a 且原文件一字节不少(数据优先)",
              info4 is None and mode4 == "a" and os.path.getsize(lp) == before,
              (mode4, info4, os.path.getsize(lp)))
        check("D18 失败原因记进 LAST_ERROR(不再静默吞掉)",
              "disk full" in (LR.LAST_ERROR or ""), LR.LAST_ERROR)
    finally:
        LR.shutil.copyfile = orig_copy

    # J) 不依赖任何文件句柄 —— 这正是绕开 WinError 32 的关键
    check("D19 plan_startup 不需要 fh 参数(旧方案就是死在拿不到句柄)",
          "fh" not in inspect.signature(LR.plan_startup).parameters,
          list(inspect.signature(LR.plan_startup).parameters))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# K) archive_now:手动归档只复制,不动 run.log
tmpA = tempfile.mkdtemp(prefix="sphgj_an_")
try:
    lpa = os.path.join(tmpA, "run.log")
    with io.open(lpa, "w", encoding="utf-8") as f:
        f.write("m" * 2048)
    ia = LR.archive_now(tmpA, keep_days=7)
    check("D20 archive_now 有内容就归档",
          bool(ia) and (ia.get("archived_to") or "").startswith("run-"), ia)
    check("D21 archive_now 不动 run.log(体积不变,要重启才截断)",
          os.path.getsize(lpa) == 2048, os.path.getsize(lpa))
    os.remove(lpa)
    check("D22 run.log 不存在时 archive_now 返回 None 且不抛",
          LR.archive_now(tmpA) is None)
finally:
    shutil.rmtree(tmpA, ignore_errors=True)

MAIN_SRC = src("main.py")
SRV_SRC = src("backend/server.py")
LR_SRC = src("backend/log_rotate.py")
_b = SRV_SRC.index("async def _log_rotate_boot():")
_e = SRV_SRC.index("async def _log_rotate_loop():", _b)
SV_BOOT = SRV_SRC[_b:_e]
check("D23 main.py 在【打开 run.log 之前】就定下打开模式(来不及改名,只能靠 w 截断)",
      MAIN_SRC.index("plan_startup(") < MAIN_SRC.index("_logf = open("))
check("D24 main.py 用 plan_startup 返回的模式打开 run.log",
      "_log_rotate_info, _log_open_mode = _LOG_ROTATE.plan_startup(" in MAIN_SRC
      and "open(_LOG_FILE, _log_open_mode" in MAIN_SRC)
check("D25 main.py 不再把句柄传进轮转逻辑(旧的 copy+truncate 已弃用)",
      "fh=_logf" not in MAIN_SRC)
check("D26 main.py 轮转在写启动标记之前(归档里不该混进新一轮标记)",
      MAIN_SRC.index("plan_startup(") < MAIN_SRC.index("启动 ====="))
check("D27 轮转判定在接管 stdout 之前(重启后也一定会归档)",
      MAIN_SRC.index("plan_startup(") < MAIN_SRC.index("_console = bool("))
check("D28 非控制台时自己 open run.log 并接管 stdout/stderr(句柄归本进程)",
      "sys.stdout = _logf" in MAIN_SRC and "sys.stderr = _logf" in MAIN_SRC
      and "sys.stdout.isatty()" in MAIN_SRC)
check("D29 轮转判定自己吞异常(失败也不影响启动)",
      "except Exception:\n        _log_open_mode = \"a\"" in MAIN_SRC)
check("D30 server 有运行中检查任务并挂到 startup",
      "async def _log_rotate_loop():" in SRV_SRC
      and "asyncio.create_task(_log_rotate_loop())" in SRV_SRC)
check("D31 超限走优雅重启(不是直接删文件)",
      "restart_mod.restart(3)" in SRV_SRC and "should_rotate_running" in SRV_SRC)
check("D32 server 提供日志状态与手动归档接口",
      '@app.get("/api/system/log")' in SRV_SRC
      and '@app.post("/api/system/log-rotate")' in SRV_SRC)
check("D33 启动 40s 后兜底归档(只复制,不截断、不碰句柄)",
      "async def _log_rotate_boot():" in SRV_SRC
      and "asyncio.create_task(_log_rotate_boot())" in SRV_SRC
      and "await asyncio.sleep(40)" in SV_BOOT
      and "copy_archive(" in SV_BOOT and "handle_of" not in SV_BOOT)
check("D34 手动归档接口支持 restart=1 顺手收缩体积",
      "restart: bool = Query(False)" in SRV_SRC and "need_restart" in SRV_SRC)
check("D35 失败原因对外可见(LAST_ERROR)",
      'LAST_ERROR = "%s: %s"' in LR_SRC and "last_error" in SRV_SRC)
_lr_doc = ast.get_docstring(ast.parse(LR_SRC)) or ""
LR_CODE = LR_SRC.replace(_lr_doc, "")      # 去掉模块 docstring,它只是解释"为什么不用"
check("D36 全程不 rename(Windows 下必报 WinError 32)",
      "os.replace(" not in LR_CODE and "shutil.move(" not in LR_CODE)

# handle_of 仍保留:诊断接口靠它判断"有没有本进程句柄"
tmp2 = tempfile.mkdtemp(prefix="sphgj_h_")
try:
    other = io.open(os.path.join(tmp2, "other.log"), "w", encoding="utf-8")
    try:
        check("D37 handle_of 对无关句柄返回 None",
              LR.handle_of(tmp2) is None, LR.handle_of(tmp2))
    finally:
        other.close()
    sys.stdout.flush()
    check("D38 handle_of 不会把当前 stdout 误判成 run.log(路径不同)",
          LR.handle_of(ROOT) is None, LR.handle_of(ROOT))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)


# ---- NUL 空洞坑(2026-11-07 母本实战:截断了但文件还是 22.77MB) ----
RST_SRC = src("backend/restart.py")
check("D39 接管前把旧句柄位置归零(否则 C 层写 fd 1 会把文件撑出 NUL 空洞)",
      MAIN_SRC.index("sys.stdout.seek(0)")
      < MAIN_SRC.index("open(_LOG_FILE, _log_open_mode"))
check("D40 restart 助手不再把 run.log 当子进程 stdout(O_APPEND 会丢,位置停在旧末尾)",
      'logf = open(req["log"], "a"' not in RST_SRC
      and "run.boot.log" in RST_SRC)
check("D42 控制台模式不抢 stdout(开发者还要看到屏幕输出)",
      "if _console:" in MAIN_SRC and "日志上屏" in MAIN_SRC)
# ---- HELPER_SOURCE 是字符串:必须抽出来真的 compile 一遍 ----
_hs = RST_SRC.split("HELPER_SOURCE = r\'\'\'")[1].split("\n\'\'\'")[0]
check("D41 子进程仍留了独立的启动日志(import 阶段崩溃有据可查)",
      '"run.boot.log"' in _hs and "boot_log = os.path.join(_root," in _hs)
_hs_ok, _hs_err = True, ""
try:
    compile(_hs, "<helper>", "exec")
except SyntaxError as _e:
    _hs_ok, _hs_err = False, str(_e)
check("D43 restart_helper 源码能编译(它是字符串,平时没人编译它)", _hs_ok, _hs_err)

_tree = ast.parse(_hs)
_defined = set()
for _n in ast.walk(_tree):
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _defined.add(_n.name)
    if isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Store):
        _defined.add(_n.id)
    if isinstance(_n, ast.arg):
        _defined.add(_n.arg)
    if isinstance(_n, ast.Import):
        for _a in _n.names:
            _defined.add(_a.asname or _a.name.split(".")[0])
    if isinstance(_n, ast.ImportFrom):
        for _a in _n.names:
            _defined.add(_a.asname or _a.name)
    if isinstance(_n, ast.ExceptHandler) and _n.name:
        _defined.add(_n.name)
_used = {_n.id for _n in ast.walk(_tree)
         if isinstance(_n, ast.Name) and isinstance(_n.ctx, ast.Load)}
_free = sorted(_used - _defined - set(dir(builtins)))
check("D44 restart_helper 没有未定义的全局变量(母本曾因此崩掉重启助手)", not _free, _free)


# ============================================================
# E. 启动首检走纯 HTTP(母本 20:02 实测:恢复账号全绕过去开了大屏页)
# ============================================================
print("=== E. 启动首检走纯 HTTP ===")
InitCheck = extract("backend/account_manager.py", "_initial_live_check", AM_NS)


class FakeInit:
    def __init__(self, cfg=None, http_ret=None):
        self.logged_in = True
        self.config = cfg or {}
        self.context = object()          # 有 context,证明"不开页"不是因为没 context
        self._http_ret = http_ret
        self.calls = []

    async def _http_live_check(self):
        self.calls.append("http")
        return self._http_ret

    async def _open_and_detect_live(self):
        self.calls.append("open")
        return True

    async def release_idle_browser(self):
        self.calls.append("release")


async def _e1():
    f = FakeInit({}, True)
    await InitCheck(f)
    check("E1 默认配置 + 纯 HTTP 判在播 -> 绝不开页", f.calls == ["http"], f.calls)

    f = FakeInit({}, False)
    await InitCheck(f)
    check("E2 纯 HTTP 判未播 -> 也不开页,只回收浏览器",
          f.calls == ["http", "release"], f.calls)

    f = FakeInit({}, None)
    await InitCheck(f)
    check("E3 纯 HTTP 不可用(无规格) -> 退回开页学一次",
          f.calls == ["http", "open", "release"], f.calls)

    f = FakeInit({"live_use_page": True}, True)
    await InitCheck(f)
    check("E4 live_use_page=True -> 直接开页(开关仍可用)",
          f.calls == ["open", "release"], f.calls)

    f = FakeInit({}, True)
    f.logged_in = False
    await InitCheck(f)
    check("E5 未登录 -> 什么都不做", f.calls == [], f.calls)


asyncio.run(_e1())

AM_SRC = src("backend/account_manager.py")
check("E6 _http_live_check 里有三分支语义(True/False/None)",
      "async def _http_live_check(self):" in AM_SRC
      and "return True" in AM_SRC and "return False" in AM_SRC)
check("E7 _http_live_check 自己绝不建页面(纯 HTTP 的全部意义)",
      "new_page()" not in AM_SRC.split("async def _http_live_check(self):")[1]
      .split("async def _live_loop(self):")[0])
check("E8 _live_loop 的判据不再要求 context(纯 HTTP 账号没有 context)",
      "if self.logged_in and self.context:" not in AM_SRC
      and "or lf.page is None" in AM_SRC)

print("\n================ 结果 ================")
print("PASS %d / FAIL %d" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
