# -*- coding: utf-8 -*-
"""rev74 一次性补丁：远程串流多标签冲突修复 + 会话上限可配。

同一文件改多处 -> 整文件读入、逐条 assert 唯一命中、一次写回 + 自校验。
两个目标文件都是 LF，直接按 LF 匹配（保持原换行，避免破坏基线校验）。

改动清单
--------
1. backend/remote_view.py
   - DEFAULT_MAX_SESSIONS 2 -> 4，且可从 config.json 的 remote.max_sessions 读
   - 新增 SessionOccupied；RemoteSession 记 owner（持有它的 WebSocket）
   - RemoteRegistry.open(owner=, takeover=)：同账号第二个连接默认拒绝，
     view=dashboard 走优雅接管；踢旧按 created_at 且通知旧 WS；open 失败自清理
   - 新增 release()：只在「session 仍存在 且 owner 仍是自己」时收尾
2. backend/server.py
   - remote_ws 传 owner/takeover；占用时发 t=error 明确提示（复用前端现有
     error 分支，不必改前端 dist）；finally 用 release 替代无条件 close
   - 顺手修掉原 finally 里 sess=None 时 AttributeError 导致 ws.close 不执行
   - /api/remote/sessions 回报 max_sessions
3. tools/test_rev67_view.py（兼容性，不改任何断言）
   - FakeRegistry.open 接受 owner/takeover 但不记进 calls
   - FakeRegistry.release 记进 closed（沿用 17 号断言的口径）
   - build_open_part 的 ns 注入 SessionOccupied
"""
import io
import os

ROOT = r"D:\SPHGJ"
RV = os.path.join(ROOT, "backend", "remote_view.py")
SV = os.path.join(ROOT, "backend", "server.py")
T67 = os.path.join(ROOT, "tools", "test_rev67_view.py")

OLD_REGISTRY = '''class RemoteRegistry:
    """会话登记:同账号复用,超并发踢旧的。"""

    def __init__(self, manager, config, storage=None):
        self.manager = manager
        self.config = config
        self.storage = storage
        self.sessions = {}

    def get(self, account_id):
        s = self.sessions.get(account_id)
        if s and not s.closed:
            return s
        return None

    async def open(self, account_id, url=None, idle_timeout_sec=None):
        # 全局只留 MAX_SESSIONS 路
        alive = [s for s in self.sessions.values() if not s.closed]
        while len(alive) >= MAX_SESSIONS:
            old = alive.pop(0)
            logger.info(f"[remote] 会话数已满,先关掉 {old.account_id}")
            await old.close("kicked")
            self.sessions.pop(old.account_id, None)
        s = self.get(account_id)
        if s is not None:
            # rev66: 页面已关 / CDP 掉了的「僵尸会话」不能复用,否则前端
            # 连上就报「建立会话失败」,而且这个会话永远没人收尾。
            try:
                if (s.page is not None and not s.page.is_closed()
                        and s.cdp is not None and s.ctx is not None):
                    await s.refresh_url()
                    return s, True
            except Exception:
                pass
            logger.info(f"[remote] 复用会话已失效,重建 {account_id}")
            await self.close(account_id, "stale")
        s = RemoteSession(self.manager, account_id, self.config, self.storage,
                          idle_timeout_sec=idle_timeout_sec or IDLE_TIMEOUT_SEC)
        meta = await s.open(url)
        self.sessions[account_id] = s
        return s, False

    async def close(self, account_id, reason="closed"):
        s = self.sessions.pop(account_id, None)
        if s is not None:
            await s.close(reason)

    async def close_all(self):
        for aid in list(self.sessions):
            await self.close(aid, "shutdown")

    def snapshot(self):
        return [{"account_id": s.account_id, "url": s.url,
                 "idle_sec": int(time.time() - s.last_active),
                 "created_at": s.created_at} for s in self.sessions.values() if not s.closed]
'''

NEW_REGISTRY = '''class RemoteRegistry:
    """会话登记:同账号单路 + 归属管理(rev74)。

    rev74 之前同账号多标签会复用同一个 RemoteSession:两个 WS 从同一个
    maxsize=2 的帧队列抢帧(各只拿到一半画面),而且任一 WS 的 finally 都
    无条件 close(account),关掉一个标签页就把另一路一起断了。现在:
      * 每个 session 记 owner(持有它的 WebSocket);同账号第二个连接默认
        拒绝(SessionOccupied),只有 view=dashboard 允许接管并踢掉旧 WS;
      * release() 只在「会话仍是当初那个 且 owner 仍是自己」时收尾;
      * 上限可配(config.remote.max_sessions),踢旧按 created_at 并通知旧 WS。
    """

    def __init__(self, manager, config, storage=None):
        self.manager = manager
        self.config = config
        self.storage = storage
        self.sessions = {}

    def max_sessions(self):
        """同时最多几路串流;非法/缺省值一律收敛到 DEFAULT_MAX_SESSIONS。"""
        try:
            n = int(((self.config or {}).get("remote") or {}).get("max_sessions")
                    or DEFAULT_MAX_SESSIONS)
        except (TypeError, ValueError, AttributeError):
            n = DEFAULT_MAX_SESSIONS
        return max(1, min(16, n))

    def get(self, account_id):
        s = self.sessions.get(account_id)
        if s and not s.closed:
            return s
        return None

    def is_occupied(self, account_id, owner):
        s = self.get(account_id)
        return bool(s and getattr(s, "owner", None) is not None
                    and s.owner is not owner)

    @staticmethod
    async def _evict_owner(s):
        """接管/踢旧时让旧 WS 立刻断开,避免它继续抢帧或误关新会话。"""
        w = getattr(s, "owner", None)
        s.owner = None
        if w is None:
            return
        try:
            await w.send_json({"t": "closed", "reason": "kicked",
                               "message": "这个账号已在另一个标签页里打开,当前连接被接管"})
        except Exception:
            pass
        try:
            await w.close()
        except Exception:
            pass

    async def open(self, account_id, url=None, idle_timeout_sec=None,
                   owner=None, takeover=False):
        # 1) 同账号占用先判:不能先踢掉别人的会话再报错
        s = self.get(account_id)
        if s is not None and self.is_occupied(account_id, owner):
            if not takeover:
                raise SessionOccupied(
                    "该账号已在另一个标签页里打开远程串流,请先关闭那个标签页再重开")
            # 直播大屏按钮本身就是新标签页(rev67),允许它接管旧连接。
            await self._evict_owner(s)

        # 2) 全局上限:按创建时间踢最旧的;排除本账号(它可能正要被复用),
        #    否则复用自己的会话时会把自己踢掉,大屏画面直接丢失。
        others = sorted((x for x in self.sessions.values()
                         if not x.closed and x.account_id != account_id),
                        key=lambda x: getattr(x, "created_at", 0))
        while len(others) >= self.max_sessions():
            old = others.pop(0)
            logger.info(f"[remote] 会话数已满,先关掉 {old.account_id}")
            await self._evict_owner(old)
            await old.close("kicked")
            self.sessions.pop(old.account_id, None)

        s = self.get(account_id)
        if s is not None:
            # rev66: 页面已关 / CDP 掉了的「僵尸会话」不能复用,否则前端
            # 连上就报「建立会话失败」,而且这个会话永远没人收尾。
            try:
                if (s.page is not None and not s.page.is_closed()
                        and s.cdp is not None and s.ctx is not None):
                    await s.refresh_url()
                    s.owner = owner
                    return s, True
            except Exception:
                pass
            logger.info(f"[remote] 复用会话已失效,重建 {account_id}")
            await self.close(account_id, "stale")
        s = RemoteSession(self.manager, account_id, self.config, self.storage,
                          idle_timeout_sec=idle_timeout_sec or IDLE_TIMEOUT_SEC)
        try:
            await s.open(url)
        except Exception:
            # 启动浏览器/注入登录态失败时,别留下半死会话(rev66 的僵尸来源)
            await s.close("open_failed")
            raise
        s.owner = owner
        self.sessions[account_id] = s
        return s, False

    async def close(self, account_id, reason="closed"):
        s = self.sessions.pop(account_id, None)
        if s is not None:
            await s.close(reason)

    async def release(self, account_id, sess, owner, reason="closed"):
        """rev74: WS 结束时只收尾「自己仍持有」的那一路。

        被接管 / 被踢旧 / 已被重建的旧 WS 走到这里时,owner 或 session
        对象都不再匹配,直接放过,不会把新标签页的串流一起关掉。
        """
        if sess is None:
            return
        if self.sessions.get(account_id) is not sess:
            return
        if getattr(sess, "owner", None) is not owner:
            return
        await self.close(account_id, reason)

    async def close_all(self):
        for aid in list(self.sessions):
            await self.close(aid, "shutdown")

    def snapshot(self):
        return [{"account_id": s.account_id, "url": s.url,
                 "idle_sec": int(time.time() - s.last_active),
                 "created_at": s.created_at,
                 "owned": getattr(s, "owner", None) is not None}
                for s in self.sessions.values() if not s.closed]
'''

RV_PATCHES = [
    ("""MAX_SESSIONS = 2                # 同时最多两路串流:一路别人开了还能再开一路,
                                # 又不至于把主机压垮(每个 context 约 150-300MB)
""",
     """DEFAULT_MAX_SESSIONS = 4  # rev74: 放宽到 4 路,可在 config.json 的 remote.max_sessions 调
MAX_SESSIONS = DEFAULT_MAX_SESSIONS          # 兼容别名(老脚本可能引用)
                                # 每路 context 约 150-300MB,上限由 max_sessions() 兜底
""",
     "max sessions const"),

    ('''class RemoteSession:
    """一路串流会话:一个浏览器 context + 一个 page + 一条 CDP 通道。"""
''',
     '''class SessionOccupied(RuntimeError):
    """rev74: 同账号已有一路活跃串流,且本次连接不允许接管。"""


class RemoteSession:
    """一路串流会话:一个浏览器 context + 一个 page + 一条 CDP 通道。"""
''',
     "occupied exception"),

    ("""        self._last_frame_ts = 0.0
""",
     """        self._last_frame_ts = 0.0
        self.owner = None              # rev74: 当前持有这路串流的 WebSocket
""",
     "session owner field"),

    ("""        self.closed = True
        self.close_reason = reason
""",
     """        self.closed = True
        self.close_reason = reason
        self.owner = None              # rev74: 旧 WS 的 release 不再误关新会话
""",
     "close clears owner"),

    (OLD_REGISTRY, NEW_REGISTRY, "registry rewrite"),
]

SV_PATCHES = [
    ("""from .remote_view import RemoteRegistry, DASHBOARD_IDLE_SEC
""",
     """from .remote_view import RemoteRegistry, DASHBOARD_IDLE_SEC, SessionOccupied
""",
     "import"),

    ("""    sess = None
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        sess, reused = await remote_registry.open(account, target_url or None,
                                                   idle_timeout_sec=idle_sec)
        if reused:
""",
     """    sess = None
    skip_cleanup = False
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        try:
            # rev74: owner 用于判定「关一个标签页会不会把整路会话关掉」;
            # 只有 view=dashboard(直播大屏按钮本来就是新标签页)允许接管旧连接。
            sess, reused = await remote_registry.open(
                account, target_url or None, idle_timeout_sec=idle_sec,
                owner=ws, takeover=(view == "dashboard"))
        except SessionOccupied as e:
            # 同账号第二个标签页:明确拒绝,前端复用现有 error 分支显示 message,
            # 不必改前端 dist。绝不能让它复用同一路串流去抢帧。
            skip_cleanup = True
            try:
                await ws.send_json({"t": "error", "message": str(e)})
            except Exception:
                pass
            return
        if reused:
""",
     "remote_ws open"),

    ("""    except Exception as e:
        logger.warning(f"[remote] 建立会话失败 {account}: {e}")
        # rev66: open() 可能已经把会话登记进 registry(日志里出现过
        # 「会话已开启」紧接「建立会话失败」),不清理会留下僵尸会话。
        try:
            await remote_registry.close(account, "open_failed")
        except Exception:
            pass
""",
     """    except Exception as e:
        logger.warning(f"[remote] 建立会话失败 {account}: {e}")
        # rev74: 已在下面发出 error,finally 不要再补一条 closed;且只清理
        # 自己持有的会话,别误关别人正在用的那一路(open 失败的自清理已搬进
        # RemoteRegistry.open,不会再留僵尸会话)。
        skip_cleanup = True
        try:
            await remote_registry.release(account, sess, ws, "open_failed")
        except Exception:
            pass
""",
     "remote_ws except"),

    ("""    finally:
        reason = sess.close_reason or ("idle" if sess.closed else "client")
        try:
            await remote_registry.close(account, reason)
        except Exception:
            pass
        try:
            await ws.send_json({"t": "closed", "reason": reason})
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
""",
     """    finally:
        # rev74: 被拒绝/建会话失败的连接(skip_cleanup)不碰 registry;其余只在
        # 「会话仍是自己持有」时收尾,避免旧 WS 把接管后的新标签页一起关掉。
        if not skip_cleanup:
            reason = (getattr(sess, "close_reason", "") or
                      ("idle" if getattr(sess, "closed", False) else
                       ("open_failed" if sess is None else "client")))
            try:
                await remote_registry.release(account, sess, ws, reason)
            except Exception:
                pass
            try:
                await ws.send_json({"t": "closed", "reason": reason})
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
""",
     "remote_ws finally"),

    ("""    return {"items": remote_registry.snapshot()}
""",
     """    return {"items": remote_registry.snapshot(),
            "max_sessions": remote_registry.max_sessions()}
""",
     "sessions api"),

    ("""        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
""",
     """        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # rev74: 客户端正常断开时 reader() 会抛 WebSocketDisconnect;不 consume
        # 已完成 task 的异常,asyncio 会刷 "Task exception was never retrieved"。
        # 多标签页场景关标签页很频繁,这条噪音必须收掉。
        for d in done:
            try:
                exc = d.exception()
            except asyncio.CancelledError:
                continue
            if exc is not None:
                logger.debug(f"[remote] WS 子任务结束: {type(exc).__name__}: {exc}")
        for p in pending:
            p.cancel()
""",
     "consume done task exception"),
]

T67_PATCHES = [
    ("""    async def open(self, account, url=None, idle_timeout_sec=None):
        self.calls.append((account, url, idle_timeout_sec))
""",
     """    async def open(self, account, url=None, idle_timeout_sec=None,
                   owner=None, takeover=False):
        # rev74: 新参数不记进 calls,保持 6/15/16 号断言的口径
        self.calls.append((account, url, idle_timeout_sec))
""",
     "FakeRegistry.open signature"),

    ("""    async def close(self, account, reason):
        self.closed.append((account, reason))
""",
     """    async def close(self, account, reason):
        self.closed.append((account, reason))

    async def release(self, account, sess, owner, reason):
        # rev74: 17 号断言看的是 closed 里的 open_failed
        self.closed.append((account, reason))
""",
     "FakeRegistry.release"),

    ("""    ns = {"manager": manager, "remote_registry": registry, "logger": Log(),
          "DASHBOARD_IDLE_SEC": rv.DASHBOARD_IDLE_SEC}
""",
     """    ns = {"manager": manager, "remote_registry": registry, "logger": Log(),
          "DASHBOARD_IDLE_SEC": rv.DASHBOARD_IDLE_SEC,
          "SessionOccupied": rv.SessionOccupied}
""",
     "build_open_part namespace"),
]


def apply(path, patches):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        s = f.read()
    assert "\r\n" not in s, "%s 不是纯 LF,先处理换行再打补丁" % path
    for old, new, tag in patches:
        n = s.count(old)
        if n == 0 and new in s:
            continue          # 已打过补丁,重跑幂等
        assert n == 1, "%s / %s 命中 %d 次(应为 1)" % (os.path.basename(path), tag, n)
        s = s.replace(old, new, 1)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    return s


def main():
    rv = apply(RV, RV_PATCHES)
    sv = apply(SV, SV_PATCHES)
    t67 = apply(T67, T67_PATCHES)

    # 自校验:每条改动的关键特征都在,且旧特征已消失
    checks = [
        ("RV 默认上限 4", "DEFAULT_MAX_SESSIONS = 4" in rv),
        ("RV 兼容别名", "MAX_SESSIONS = DEFAULT_MAX_SESSIONS" in rv),
        ("RV 异常类", "class SessionOccupied(RuntimeError):" in rv),
        ("RV owner 字段", "self.owner = None              # rev74" in rv),
        ("RV 可配上限", "def max_sessions(self):" in rv and 'get("max_sessions")' in rv),
        ("RV 占用检测", "def is_occupied(self, account_id, owner):" in rv),
        ("RV 踢旧通知", "async def _evict_owner(s):" in rv),
        ("RV 接管参数", "owner=None, takeover=False):" in rv),
        ("RV open 失败自清理", 'await s.close("open_failed")' in rv),
        ("RV release", "async def release(self, account_id, sess, owner, reason=" in rv),
        ("RV 旧无条件踢法已删", "while len(alive) >= MAX_SESSIONS:" not in rv),
        ("SV import", "DASHBOARD_IDLE_SEC, SessionOccupied" in sv),
        ("SV owner/takeover", "owner=ws, takeover=(view == \"dashboard\")" in sv),
        ("SV occupied 分支", "except SessionOccupied as e:" in sv),
        ("SV consume done 异常", "exc = d.exception()" in sv),
        ("RV 占用提示文案", "该账号已在另一个标签页里打开远程串流" in rv),
        ("SV occupied 发 error", '{"t": "error", "message": str(e)}' in sv),
        ("SV 用 release", "await remote_registry.release(account, sess, ws, reason)" in sv),
        ("SV 不再无条件 close", "await remote_registry.close(account, reason)" not in sv),
        ("SV finally 防 sess=None", 'getattr(sess, "close_reason", "")' in sv),
        ("SV sessions 回报上限", '"max_sessions": remote_registry.max_sessions()' in sv),
        ("T67 FakeRegistry 新签名", "owner=None, takeover=False):" in t67),
        ("T67 FakeRegistry.release", "async def release(self, account, sess, owner, reason):" in t67),
        ("T67 ns 注入", '"SessionOccupied": rv.SessionOccupied' in t67),
    ]
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", n))
    print("\nSELFCHECK %s (%d/%d)" % ("PASS" if not bad else "FAIL",
                                      len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
