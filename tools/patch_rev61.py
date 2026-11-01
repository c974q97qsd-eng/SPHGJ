# -*- coding: utf-8 -*-
"""rev61 补丁：回评失败被静默吞掉 + exportId 缺失 + 补登误配。

五处改动，逐条 assert 唯一命中，写完自校验。
"""
import io
import sys

ROOT = r"D:\SPHGJ\backend"


def patch(name, patches):
    p = ROOT + "\\" + name
    s = io.open(p, encoding="utf-8").read()
    for i, (old, new, tag) in enumerate(patches, 1):
        n = s.count(old)
        assert n == 1, "[%s#%d %s] 命中 %d 次(要 1)" % (name, i, tag, n)
        s = s.replace(old, new, 1)
    io.open(p, "w", encoding="utf-8", newline="").write(s)
    print("  OK", name, "->", len(patches), "处")


# ---------------- api_client.py ----------------
API = [
    # 1) _aid 现取：浏览器路径原来用构造函数里的旧 aid
    (
        """        base = base or BASE
        page_url = page_url or PAGE_URL
        url = f"{base}/{path}?_aid={self.aid}&_pageUrl={quote(page_url)}\"""",
        """        base = base or BASE
        page_url = page_url or PAGE_URL
        # rev61: _aid 每次登录都会变,而原来只有 _post_via_http 里会纠正 —— 写操作
        # 默认走浏览器,于是拿旧 aid 发包,服务端一律回 300800 request failed。
        # 这里统一现取,两条路都用同一份最新值。
        _cur_aid = ((self._latest_account() or {}).get("_aid") or "").strip()
        if _cur_aid and _cur_aid != self.aid:
            logger.info(f"[api] _aid 已更新({str(self.aid)[:8]}… -> {_cur_aid[:8]}…) path={path}")
            self.aid = _cur_aid
        url = f"{base}/{path}?_aid={self.aid}&_pageUrl={quote(page_url)}\"""",
        "aid-refresh",
    ),
    # 2) 写操作失败打详情（下次 300800 能直接看到缺什么）
    (
        """        if self._is_failure(resp):
            self._on_failure()
        else:""",
        """        if self._is_failure(resp):
            self._on_failure()
            if is_write:
                # rev61: 写失败必须留下请求指纹。原来只有一句「请求失败(连续 N 次)」,
                # 拿不到 path/aid/关键字段,300800 查了两周都没定位。
                _keys = ("replyCommentId", "rootCommentId", "exportId", "opType",
                         "objectId", "visibleType", "stickyOp")
                logger.warning(
                    "[api] 写操作失败 path=%s aid=%s 字段=%s resp=%s",
                    path, (self.aid or "-")[:10],
                    {k: str(body.get(k))[:24] for k in _keys if k in body},
                    str(resp)[:300])
        else:""",
        "write-fail-log",
    ),
    # 3) 上层统一的成败判定
    (
        """        base = resp.get("base_resp")
        if isinstance(base, dict):
            v = base.get("ret")
            if isinstance(v, int):
                return v
        return None""",
        """        base = resp.get("base_resp")
        if isinstance(base, dict):
            v = base.get("ret")
            if isinstance(v, int):
                return v
        return None

    @staticmethod
    def failed(resp):
        \"\"\"rev61: 给【上层】判断「这一次到底成没成」。

        原来 server/auto_reply/auto_comment 三处都只审 `__err`,而视频号的业务
        失败走的是 errCode(如 300800 request failed),响应里根本没有 __err ——
        于是失败被当成成功:mark_replied 照写、待确认照记,客户没收到回复,
        系统却认为已经回过了,下一轮也不会再试。线上 01:36/02:20/03:03/03:05/
        04:23 五次 300800 全是这么被吞掉的。
        daily_write_limit 也算失败(确实没发出去),调用方按 502 提示即可。
        \"\"\"
        if not isinstance(resp, dict):
            return True
        if resp.get("__err"):
            return True
        return WxApiClient._err_code(resp) not in (0, None)""",
        "failed()",
    ),
    # 4) reply_comment 补 exportId
    (
        '''    async def reply_comment(self, reply_comment_id, content, root_comment_id=None):
        """回复评论。rootCommentId 缺省=replyCommentId(回复一级评论)。"""
        return await self._post("comment/create_comment", self._body(
            replyCommentId=reply_comment_id, content=content,
            clientId=str(uuid.uuid4()),
            rootCommentId=root_comment_id or reply_comment_id))''',
        '''    async def reply_comment(self, reply_comment_id, content, root_comment_id=None,
                            export_id=None):
        """回复评论。rootCommentId 缺省=replyCommentId(回复一级评论)。

        rev61: **必须带 exportId**。不带时服务端一律回 {errCode:300800,
        errMsg:"request failed"}。对照证据(同一批账号、同一个接口
        comment/create_comment):
            post_comment  带 exportId + comment{}  -> own_comments 成功 446 条
            reply_comment 不带 exportId            -> 成功 0 条(4 条全是补登误配)
        export_id 缺省时由调用方从 comments 表按 comment_id 反查。
        """
        return await self._post("comment/create_comment", self._body(
            replyCommentId=reply_comment_id, content=content,
            clientId=str(uuid.uuid4()),
            rootCommentId=root_comment_id or reply_comment_id,
            exportId=export_id or ""))''',
        "reply-exportId",
    ),
]

# ---------------- server.py ----------------
SRV = [
    (
        """    resp = await w.api.reply_comment(comment_id, body.content)
    if not resp or resp.get("__err"):
        raise HTTPException(502, f"回复失败: {resp}")
    storage.mark_replied(comment_id)""",
        """    # rev61: exportId 必须带(见 api_client.reply_comment);成败判定改用
    # api.failed() —— 只审 __err 会把 errCode=300800 当成功,标记已回复后
    # 再也不会重试,客户永远收不到。
    _cinfo = storage.get_comment(comment_id) or {}
    _exp = _cinfo.get("export_id")
    resp = await w.api.reply_comment(comment_id, body.content, export_id=_exp)
    if w.api.failed(resp):
        logger.warning("手动回复失败 comment=%s export=%s resp=%s",
                       comment_id, (_exp or "-")[:18], str(resp)[:300])
        raise HTTPException(502, f"回复失败: {resp}")
    storage.mark_replied(comment_id)""",
        "manual-reply-check",
    ),
    (
        """    _cinfo = storage.get_comment(comment_id) or {}
    _fid = _cinfo.get("finder_id") or fid          # 以评论行上的归属为准(更权威)
    _exp = _cinfo.get("export_id")
    _aid = w.account["id"]""",
        """    _fid = _cinfo.get("finder_id") or fid          # 以评论行上的归属为准(更权威)
    _aid = w.account["id"]""",
        "dedup-cinfo",
    ),
]

# ---------------- auto_reply.py ----------------
AUTOR = [
    (
        """        try:
            resp = await self.api.reply_comment(cid, reply_text)
            if resp and not resp.get("__err"):
                await asyncio.to_thread(self.storage.mark_replied, cid)""",
        """        try:
            # rev61: exportId 必须带;不带时服务端回 300800,原来被当成成功。
            if not export_id:
                _row = await asyncio.to_thread(self.storage.get_comment, cid) or {}
                export_id = _row.get("export_id")
            resp = await self.api.reply_comment(cid, reply_text, export_id=export_id)
            if not self.api.failed(resp):
                await asyncio.to_thread(self.storage.mark_replied, cid)""",
        "auto-reply-check",
    ),
]

# ---------------- auto_comment.py ----------------
AUTOC = [
    (
        """            resp = await self.api.post_comment(export_id, content)
            if not resp or resp.get("__err"):
                logger.warning(f"[{self.account['id']}] 自动评论失败 {export_id}: {resp}")
                return False""",
        """            resp = await self.api.post_comment(export_id, content)
            # rev61: 与回评同一套判定(errCode 业务失败也算失败)
            if self.api.failed(resp):
                logger.warning(f"[{self.account['id']}] 自动评论失败 {export_id}: {resp}")
                return False""",
        "auto-comment-check",
    ),
    (
        """            pin_resp = await self.api.pin_comment(export_id, comment_id)
            if pin_resp and not pin_resp.get("__err"):""",
        """            pin_resp = await self.api.pin_comment(export_id, comment_id)
            if not self.api.failed(pin_resp):""",
        "pin-check",
    ),
]

# ---------------- storage.py ----------------
STO = [
    (
        """            rows = c.execute(
                "SELECT id, finder_id, account_id, export_id, content, via FROM own_comment_pending"
                + (" WHERE finder_id=?" if finder_id else ""),
                (finder_id,) if finder_id else ()).fetchall()""",
        """            rows = c.execute(
                "SELECT id, finder_id, account_id, export_id, content, via, created_at "
                "FROM own_comment_pending"
                + (" WHERE finder_id=?" if finder_id else ""),
                (finder_id,) if finder_id else ()).fetchall()""",
        "pending-select-created_at",
    ),
    (
        """            for pid, fid, acc, exp, content, via in rows:""",
        """            for pid, fid, acc, exp, content, via, created_at in rows:""",
        "pending-unpack",
    ),
    (
        """                row = c.execute(
                    \"\"\"SELECT comment_id FROM comments
                        WHERE finder_id=? AND content=?
                          AND (?='' OR export_id=?)
                          AND deleted=0 AND COALESCE(comment_id,'')<>''
                          AND comment_id NOT IN
                              (SELECT comment_id FROM own_comments WHERE COALESCE(comment_id,'')<>'')
                        ORDER BY create_time ASC, id ASC LIMIT 1\"\"\",
                    (fid, content, exp or "", exp or "")).fetchone()""",
        """                # rev61: 加时间窗 + 取【最近】的一条。
                # 原来不限时间还 ORDER BY create_time ASC,会认到几个月前的同话术
                # 老评论 —— 线上实锤:10-14/10-25 两条待确认,被补登成 10-21 的两条
                # auto_comment(话术都是「点头像，进入直播间领取优惠券下单！」)。
                # 客户不会复述我方话术,但【我方自动评论】会,所以只靠内容匹配必然串。
                lo, hi = self._pending_time_window(created_at)
                row = c.execute(
                    \"\"\"SELECT comment_id FROM comments
                        WHERE finder_id=? AND content=?
                          AND (?='' OR export_id=?)
                          AND create_time BETWEEN ? AND ?
                          AND deleted=0 AND COALESCE(comment_id,'')<>''
                          AND comment_id NOT IN
                              (SELECT comment_id FROM own_comments WHERE COALESCE(comment_id,'')<>'')
                        ORDER BY create_time DESC, id DESC LIMIT 1\"\"\",
                    (fid, content, exp or "", exp or "", lo, hi)).fetchone()""",
        "pending-time-window",
    ),
    (
        """    def pending_own_comment_count(self, finder_id=None):""",
        """    @staticmethod
    def _pending_time_window(created_at, before=600, after=1800):
        \"\"\"待确认登记的时间窗(unix 秒)。登记时刻前 10 分钟 ~ 后 30 分钟。

        抓取有延迟,所以窗口向后放宽;但绝不允许跨天 —— 跨天就会认到老的
        同话术评论。created_at 解析不出来时退化为「不限时间」。
        \"\"\"
        try:
            ts = int(datetime.fromisoformat(str(created_at).strip()).timestamp())
        except Exception:
            return 0, 2 ** 31
        return ts - int(before), ts + int(after)

    def pending_own_comment_count(self, finder_id=None):""",
        "time-window-helper",
    ),
]


def main():
    print("补丁 rev61:")
    patch("api_client.py", API)
    patch("server.py", SRV)
    patch("auto_reply.py", AUTOR)
    patch("auto_comment.py", AUTOC)
    patch("storage.py", STO)

    # ---- 自校验 ----
    checks = [
        ("api_client.py", "_cur_aid = ((self._latest_account() or {}).get", True),
        ("api_client.py", "def failed(resp):", True),
        ("api_client.py", "exportId=export_id or \"\"", True),
        ("api_client.py", "[api] 写操作失败 path=", True),
        ("server.py", "if w.api.failed(resp):", True),
        ("server.py", "resp = await w.api.reply_comment(comment_id, body.content, export_id=_exp)", True),
        ("server.py", "    _cinfo = storage.get_comment(comment_id) or {}\n    _fid = _cinfo.get(\"finder_id\")", False),
        ("auto_reply.py", "if not self.api.failed(resp):", True),
        ("auto_comment.py", "if self.api.failed(resp):", True),
        ("auto_comment.py", "if not self.api.failed(pin_resp):", True),
        ("storage.py", "def _pending_time_window", True),
        ("storage.py", "create_time BETWEEN ? AND ?", True),
        ("storage.py", "ORDER BY create_time DESC, id DESC LIMIT 1", True),
        ("storage.py", "for pid, fid, acc, exp, content, via, created_at in rows:", True),
    ]
    bad = 0
    for name, needle, want in checks:
        s = io.open(ROOT + "\\" + name, encoding="utf-8").read()
        got = needle in s
        ok = (got == want)
        if not ok:
            bad += 1
        print(("  PASS " if ok else "  FAIL ") + name + " :: " + needle[:60]
              + ("" if ok else " (期望 %s 实际 %s)" % (want, got)))
    print("SELFCHECK", "PASS" if bad == 0 else "FAIL(%d)" % bad)
    sys.exit(1 if bad else 0)


main()
