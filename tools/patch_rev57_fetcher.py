# -*- coding: utf-8 -*-
"""rev57 补丁:backend/live_fetcher.py(5 处改动,一次性原子写入)。

改同一文件多处必须走一次性补丁脚本 —— 连发多条 Edit 只有最后一条真正落盘。
每条替换先 assert 唯一命中,写完立刻自校验,不信工具回执。
"""
import io
import sys

P = r"D:\SPHGJ\backend\live_fetcher.py"
s = io.open(P, encoding="utf-8").read()
orig = s

PATCHES = []


def patch(tag, old, new):
    PATCHES.append((tag, old, new))


# ---------- 改动 1:__init__ 新增两个字段 ----------
patch(
    "1/5 __init__ 字段",
    """        self._dom_dumped = False        # 页面文本只落盘一次(诊断用)\n""",
    """        self._dom_dumped = False        # 页面文本只落盘一次(诊断用)
        # rev57: 看板字段缺失定位用
        self.live_start_ts = None       # check_live_status.createTime(开播时刻,Unix 秒)
        self._empty_info_warn_ts = 0.0  # get_live_info 返回空 data 的告警限流(秒)
""",
)

# ---------- 改动 2:poll 重放顺序 status -> info ----------
patch(
    "2/5 poll 顺序",
    """        # 主动重放两个 API
        try:
            spec_info = self._learned.get("live_info")
            if spec_info:
                txt = await self._fetch_replay(spec_info)
                if txt:
                    await self._parse_live_info(txt)
            spec_status = self._learned.get("live_status")
            if spec_status:
                txt2 = await self._fetch_replay(spec_status)
                if txt2:
                    await self._parse_live_status(txt2)
            self._poll_fail_streak = 0
""",
    """        # 主动重放两个 API
        # rev57: 顺序必须是 status -> info。live_status 是本场 liveObjectId 的唯一来源,
        # info 的重放 body 要用它来重写(见 _fresh_post_data)。原来 info 在前,每一轮
        # 都拿上一轮/上次进程的旧场次 ID 去打 get_live_info -> 返回空 data -> liveStats 全空。
        try:
            spec_status = self._learned.get("live_status")
            if spec_status:
                txt2 = await self._fetch_replay(spec_status)
                if txt2:
                    await self._parse_live_status(txt2)
            spec_info = self._learned.get("live_info")
            if spec_info:
                txt = await self._fetch_replay(spec_info)
                if txt:
                    await self._parse_live_info(txt)
            self._poll_fail_streak = 0
""",
)

# ---------- 改动 3:_capture_req 落库前清空场次 ID ----------
patch(
    "3/5 _capture_req 清空场次",
    '''    def _capture_req(self, req):
        """提取请求规格(供 page.evaluate(fetch) 同源重放)。"""
        return {
            "url": req.get("url", ""),
            "method": req.get("method", "POST"),
            "headers": req.get("headers", {}) or {},
            "postData": req.get("postData"),
        }
''',
    '''    def _capture_req(self, req):
        """提取请求规格(供 page.evaluate(fetch) 同源重放)。

        rev57: postData 里的 liveObjectId 一律清空后再入库。它是"场次变量"不是"接口常量",
        冻结到 data/live_api_spec.json 就会在换场/换天后一直拿旧场次去请求,接口返回
        errCode=0 但 data 为空 -> liveStats 静默断供(2026-10-29 实测:5 账号 4 个不匹配,
        唯一匹配的那台看着正常)。重放时由 _fresh_post_data 填当前值。
        """
        post = req.get("postData")
        if post and "liveObjectId" in post:
            try:
                o = json.loads(post)
                if isinstance(o, dict) and "liveObjectId" in o:
                    o["liveObjectId"] = ""
                    post = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
        return {
            "url": req.get("url", ""),
            "method": req.get("method", "POST"),
            "headers": req.get("headers", {}) or {},
            "postData": post,
        }
''',
)

# ---------- 改动 4:_fetch_replay 重写 body + 新增 _fresh_post_data ----------
patch(
    "4/5 _fresh_post_data + _fetch_replay",
    '''    async def _fetch_replay(self, spec):
        """同源重放学到的请求(fetch,credentials:include 自动带 cookie)。返回响应体文本。"""
        js = """async (spec) => {
            const headers = Object.assign({'Content-Type':'application/json'}, spec.headers || {});
            const init = {
                method: spec.method || 'POST',
                credentials: 'include',
                headers: headers,
                body: spec.postData || null,
            };
            const r = await fetch(spec.url, init);
            return await r.text();
        }"""
        # rev21: page.evaluate 没有内建超时,页面卡死时会永久挂住整个 poll 循环
        r = await _with_timeout(self.page.evaluate(js, spec), _EVAL_TIMEOUT)
        if r is None:
            logger.debug(f"[live:{self.account_id}] fetch 重放超时/失败")
            return ""
        return r
''',
    '''    def _fresh_post_data(self, spec):
        """rev57: 重放前用【当前场次 ID】+【当前时间戳】重写缓存的请求体。

        为什么必须重写:data/live_api_spec.json 缓存的是"学到那一刻"的整段 postData,
        live_info 的 liveObjectId 被冻结成当时的场次。换场/换天后仍在重放旧 ID,
        接口返回 errCode=0 但 data 里没有 liveStats -> live_stats 永远停在旧值/空值,
        看板的 当前在线 / 累计观看 / GMV / 时长 随之消失(日志无任何报错,极难定位)。
        2026-10-29 对照实验实锤:同一 cookie/headers 只换这个 ID,旧 ID 返回 data 仅
        {multiPullStreamUrlInfos, realtimeStatus},新 ID 返回 14 个 liveStats 字段。

        判定用【解析后的顶层键】而不是字符串包含 —— check_live_status 的 postData 里
        有 getLiveStreamContext 这类嵌套对象,哪天它带上 liveObjectId,字符串判断会把
        字段注入到 check_live_status 的顶层,污染原本正常的请求。

        返回 None 表示"这轮别发":还没有本场 liveObjectId 时,发出去必然是空数据,
        发了反而会把"本轮拿到了 liveStats"这件事变成假象。
        """
        raw = spec.get("postData")
        if not raw:
            return raw
        try:
            obj = json.loads(raw)
        except Exception:
            return raw          # body 不是 JSON(理论上不存在) -> 保持原行为
        if not isinstance(obj, dict) or "liveObjectId" not in obj:
            return raw          # 顶层没有场次字段(如 check_live_status) -> 原样重放
        oid = self.live_object_id
        if not oid:
            return None
        old = obj.get("liveObjectId") or ""
        if old and str(old) != str(oid):
            logger.info(f"[live:{self.account_id}] 重放场次 ID 已刷新: {old} -> {oid}")
        obj["liveObjectId"] = str(oid)
        if "timestamp" in obj:
            obj["timestamp"] = str(int(time.time() * 1000))
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    async def _fetch_replay(self, spec):
        """同源重放学到的请求(fetch,credentials:include 自动带 cookie)。返回响应体文本。

        rev57: 不再原样重放 postData,先经 _fresh_post_data 换成本场次 ID。
        """
        body = spec.get("postData")
        if body:
            body = self._fresh_post_data(spec)
            if body is None:
                logger.debug(f"[live:{self.account_id}] 缺本场 liveObjectId,跳过该接口重放")
                return ""
        merged = dict(spec)
        merged["postData"] = body
        js = """async (spec) => {
            const headers = Object.assign({'Content-Type':'application/json'}, spec.headers || {});
            const init = {
                method: spec.method || 'POST',
                credentials: 'include',
                headers: headers,
                body: spec.postData || null,
            };
            const r = await fetch(spec.url, init);
            return await r.text();
        }"""
        # rev21: page.evaluate 没有内建超时,页面卡死时会永久挂住整个 poll 循环
        r = await _with_timeout(self.page.evaluate(js, merged), _EVAL_TIMEOUT)
        if r is None:
            logger.debug(f"[live:{self.account_id}] fetch 重放超时/失败")
            return ""
        return r
''',
)

# ---------- 改动 5a:_parse_live_info 空 data 告警 ----------
patch(
    "5a/5 _parse_live_info 空 data 告警",
    """        d = data.get("data") or {}
        stats = d.get("liveStats")
        if stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
""",
    """        d = data.get("data") or {}
        stats = d.get("liveStats")
        if not stats:
            # rev57: 空 data 是"场次 ID 不对 / 场次已结束 / 接口改版"的唯一表现(接口 errCode=0),
            # 原来静默 return,看板数据消失却毫无线索。限流 5 分钟告警一次。
            _now = time.time()
            if _now - (self._empty_info_warn_ts or 0) > 300:
                self._empty_info_warn_ts = _now
                _keys = sorted(d.keys())[:8] if isinstance(d, dict) else type(d).__name__
                logger.warning(
                    f"[live:{self.account_id}] get_live_info 返回空 data"
                    f"(errCode={data.get('errCode')}, used_id={self.live_object_id}, "
                    f"data_keys={_keys}),liveStats 不更新,"
                    f"沿用上次样本={bool(self.live_stats)}")
        if stats:
            self.live_stats = stats
            self.updated_at = datetime.now().isoformat()
            self.updated_at_ts = time.time()
""",
)

# ---------- 改动 5b:_parse_live_status 记录 createTime ----------
patch(
    "5b/5 _parse_live_status 记 createTime",
    """        d = data.get("data") or {}
        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
""",
    """        d = data.get("data") or {}
        # rev57: 记开播时刻(秒),供上层在 liveStats 缺 liveDurationInSeconds 时推算时长。
        # 现网 check_live_status 一定带 createTime(实测三账号均有,且是字符串形如 '1790000000')。
        _ct = d.get("createTime") if isinstance(d, dict) else None
        if _ct:
            try:
                _cti = int(_ct)
                self.live_start_ts = _cti // 1000 if _cti > 10 ** 11 else _cti   # 兼容毫秒
            except Exception:
                pass
        # rev45: 现网没有 get_live_info 接口(URL 采样确认),liveStats 只能指望这里。
""",
)

# ---------- 改动 5c:snapshot 带出 live_start_ts ----------
patch(
    "5c/5 snapshot 带出 live_start_ts",
    """            "status_num": self.status_num,
            "dom_state": self.dom_state,
""",
    """            "status_num": self.status_num,
            "live_start_ts": self.live_start_ts,   # rev57: 时长兜底用
            "dom_state": self.dom_state,
""",
)


for tag, old, new in PATCHES:
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次(应为 1),中止,未写盘")
        sys.exit(1)
    s = s.replace(old, new, 1)
    print(f"[ok] {tag}")

io.open(P, "w", encoding="utf-8", newline="").write(s)
print(f"\n已写回 {P}  ({len(orig)} -> {len(s)} 字节)")

# ---------- 自校验 ----------
s2 = io.open(P, encoding="utf-8").read()
CHECKS = [
    ("字段 live_start_ts", "self.live_start_ts = None" in s2),
    ("字段 _empty_info_warn_ts", "self._empty_info_warn_ts = 0.0" in s2),
    ("poll 顺序 status 先", s2.index('spec_status = self._learned.get("live_status")')
     < s2.index('spec_info = self._learned.get("live_info")')),
    ("_capture_req 清空", 'o["liveObjectId"] = ""' in s2),
    ("_fresh_post_data 存在", "def _fresh_post_data(self, spec):" in s2),
    ("_fresh_post_data 判顶层键", 'if not isinstance(obj, dict) or "liveObjectId" not in obj:' in s2),
    ("_fetch_replay 用 merged", "self.page.evaluate(js, merged)" in s2),
    ("空 data 告警", "get_live_info 返回空 data" in s2),
    ("重放场次刷新日志", "重放场次 ID 已刷新" in s2),
    ("createTime 记录", 'self.live_start_ts = _cti // 1000 if _cti > 10 ** 11 else _cti' in s2),
    ("snapshot 带出", '"live_start_ts": self.live_start_ts' in s2),
]
print()
bad = 0
for name, ok in CHECKS:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad += 0 if ok else 1
print("\nSELFCHECK", "PASS" if bad == 0 else f"FAIL({bad})")
sys.exit(1 if bad else 0)
