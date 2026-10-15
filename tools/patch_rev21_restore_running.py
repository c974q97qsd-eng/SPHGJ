# -*- coding: utf-8 -*-
"""rev21: 重启后自动恢复"关闭之前在运行中的账号"。

改动(母本 \\\\Sphgj\\sphgj 为源,同步镜像 D:\\SPHGJ):
  backend/account_manager.py
    1. AccountManager.__init__: 初始化运行意图(读 data/run_state.json)
    2. start_account: 成功入 workers 后记 intent + 落盘
    3. stop_account: 用户主动停 -> 清 intent + 落盘
       (stop_all 优雅关闭不走 stop_account,intent 保留 -> 重启后可恢复)
    4. 新增 _load_run_intent / _save_run_intent / restore_running_accounts
  backend/server.py
    5. startup 挂 _auto_restore_loop: 延迟 15s 后按 intent 逐个恢复(间隔 4s)
"""
import io
import os
import sys

UNC = r"\\Sphgj\sphgj"
MIRROR = r"D:\SPHGJ"

AM_REL = "backend/account_manager.py"
SV_REL = "backend/server.py"


def patch_account_manager(s: str) -> str:
    P = []  # (old, new, tag)

    # 1) __init__ 末尾初始化运行意图
    P.append((
        '        # rev15 P0: 定时内存归还(gc.collect + 工作集压缩),解决 Python 分配器不归还 OS 的 RSS 膨胀\n'
        '        self._memtrim = MemoryTrimmer(int(config.get("memtrim_interval_sec", 600)))\n',
        '        # rev15 P0: 定时内存归还(gc.collect + 工作集压缩),解决 Python 分配器不归还 OS 的 RSS 膨胀\n'
        '        self._memtrim = MemoryTrimmer(int(config.get("memtrim_interval_sec", 600)))\n'
        '        # rev21: 运行意图持久化 —— start/stop_account 时实时维护,\n'
        '        # 重启后 startup 按此恢复"关闭之前在跑"的账号(优雅关闭 stop_all 不清 intent)\n'
        '        self._run_intent_file = os.path.join(\n'
        '            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "run_state.json")\n'
        '        self._run_intent = self._load_run_intent()\n',
        "init_run_intent",
    ))

    # 2) start_account 成功入 workers 后记 intent
    P.append((
        '            self.workers[account_id] = w\n'
        '            # 手动启动:首检未播则进入短期开播探测(每 ~24s 查一次,最长 live_probe_window_sec 秒),\n',
        '            self.workers[account_id] = w\n'
        '            # rev21: 记录运行意图(重启后自动恢复)\n'
        '            self._run_intent.add(account_id)\n'
        '            self._save_run_intent()\n'
        '            # 手动启动:首检未播则进入短期开播探测(每 ~24s 查一次,最长 live_probe_window_sec 秒),\n',
        "start_account_intent",
    ))

    # 3) stop_account 清 intent + 4) 新方法插在 stop_account 与 open_account_browser 之间
    old_stop = (
        '    async def stop_account(self, account_id):\n'
        '        w = self.workers.pop(account_id, None)\n'
        '        if w:\n'
        '            await w.stop()\n'
        '        await self._emit("engine_status", self.status_snapshot())\n'
        '\n'
        '    async def open_account_browser(self, account_id):\n'
    )
    new_stop = (
        '    async def stop_account(self, account_id):\n'
        '        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)\n'
        '        self._run_intent.discard(account_id)\n'
        '        self._save_run_intent()\n'
        '        w = self.workers.pop(account_id, None)\n'
        '        if w:\n'
        '            await w.stop()\n'
        '        await self._emit("engine_status", self.status_snapshot())\n'
        '\n'
        '    # ---------- rev21: 运行意图持久化(重启恢复) ----------\n'
        '    def _load_run_intent(self) -> set:\n'
        '        """读取上次退出前的运行中账号集合;文件不存在/损坏返回空集。"""\n'
        '        try:\n'
        '            with open(self._run_intent_file, encoding="utf-8") as f:\n'
        '                d = json.load(f)\n'
        '            return set(d.get("running", []) or [])\n'
        '        except Exception:\n'
        '            return set()\n'
        '\n'
        '    def _save_run_intent(self):\n'
        '        """运行意图原子落盘(临时文件 + os.replace);失败仅警告不阻断业务。"""\n'
        '        try:\n'
        '            os.makedirs(os.path.dirname(self._run_intent_file), exist_ok=True)\n'
        '            tmp = self._run_intent_file + ".tmp"\n'
        '            with open(tmp, "w", encoding="utf-8") as f:\n'
        '                json.dump({"running": sorted(self._run_intent),\n'
        '                           "saved_at": time.time()}, f, ensure_ascii=False, indent=2)\n'
        '            os.replace(tmp, self._run_intent_file)\n'
        '        except Exception as e:\n'
        '            logger.warning(f"运行意图落盘失败(忽略): {e}")\n'
        '\n'
        '    async def restore_running_accounts(self, gap_sec: float = 4.0):\n'
        '        """重启后恢复关闭前在跑的账号。\n'
        '\n'
        '        - intent 由 start_account/stop_account 实时维护:用户关窗口/重启/强杀都不会丢,\n'
        '          只有用户主动点"停止"才清除 —— 语义即"关闭之前在运行中";\n'
        '        - 只恢复仍存在于 config 的账号(已删除的顺手清出 intent);\n'
        '        - 逐个错峰启动(默认间隔 4s),避免同时拉起多个 Chromium 争抢资源;\n'
        '        - 未登录/启动失败的账号移出 intent,避免每次重启空开浏览器。\n'
        '        """\n'
        '        known = {a["id"] for a in self.config.get("accounts", [])}\n'
        '        stale = self._run_intent - known\n'
        '        if stale:\n'
        '            self._run_intent -= stale\n'
        '            self._save_run_intent()\n'
        '        todo = [aid for aid in sorted(self._run_intent) if aid not in self.workers]\n'
        '        if not todo:\n'
        '            logger.info("[恢复] 无需要自动恢复的账号")\n'
        '            return\n'
        '        logger.info(f"[恢复] 重启后自动恢复 {len(todo)} 个关闭前在跑的账号: {todo}")\n'
        '        for i, aid in enumerate(todo):\n'
        '            if i:\n'
        '                await asyncio.sleep(gap_sec)\n'
        '            try:\n'
        '                await self.start_account(aid)\n'
        '                if aid not in self.workers:\n'
        '                    # 未登录/启动失败:移出 intent,避免下次重启再空试\n'
        '                    self._run_intent.discard(aid)\n'
        '                    self._save_run_intent()\n'
        '                    logger.warning(f"[恢复] {aid} 未登录或启动失败,已移出恢复列表")\n'
        '            except Exception as e:\n'
        '                logger.warning(f"[恢复] {aid} 启动异常(保留 intent,下次重启再试): {e}")\n'
        '        logger.info("[恢复] 自动恢复流程结束")\n'
        '\n'
        '    async def open_account_browser(self, account_id):\n'
    )
    P.append((old_stop, new_stop, "stop_account_and_restore"))

    for old, new, tag in P:
        n = s.count(old)
        assert n == 1, f"锚点命中 {n} 次(应为 1): {tag}"
        s = s.replace(old, new, 1)
    return s


def patch_server(s: str) -> str:
    old = (
        '    # 作品每日定时刷新(手动刷新不受影响)\n'
        '    asyncio.create_task(_posts_auto_refresh_loop())\n'
    )
    new = (
        '    # 作品每日定时刷新(手动刷新不受影响)\n'
        '    asyncio.create_task(_posts_auto_refresh_loop())\n'
        '    # rev21: 重启后恢复"关闭之前在运行中的账号"\n'
        '    # 延迟 15s:等前端 WS 连上先拿到真实状态,恢复过程经 engine_status 事件实时可见\n'
        '    async def _auto_restore_loop():\n'
        '        await asyncio.sleep(15)\n'
        '        if not manager.config.get("auto_restore_running", True):\n'
        '            logger.info("[恢复] auto_restore_running=false,跳过自动恢复")\n'
        '            return\n'
        '        try:\n'
        '            await manager.restore_running_accounts()\n'
        '        except Exception as e:\n'
        '            logger.warning(f"[恢复] 自动恢复异常: {e}")\n'
        '    asyncio.create_task(_auto_restore_loop())\n'
    )
    n = s.count(old)
    assert n == 1, f"server.py 锚点命中 {n} 次(应为 1)"
    return s.replace(old, new, 1)


def deliver(rel: str, patched: str):
    """写 UNC(同卷暂存 + rename 覆盖,绕开'已存在文件只读'限制)并同步本地镜像。

    关键经验(2026-10-15 实测):UNC 上重复使用同一个暂存文件名会踩僵尸锁
    (第一次 rename 失败留下的暂存文件处于半删除状态,之后每次覆盖它都会让
    后续 rename 被拒 WinError 5)。解法:暂存名带唯一后缀 + 跑前 best-effort 清理。
    """
    unc_path = os.path.join(UNC, rel.replace("/", "\\"))
    with io.open(unc_path, "rb") as f:
        original = f.read()
    assert len(original) > 0, f"{unc_path} 读取为空"
    import time as _time
    import uuid as _uuid
    tag = f"{int(_time.time())}_{_uuid.uuid4().hex[:6]}"
    stage = unc_path + f".rev21_tmp_{tag}"
    # best-effort 清理历史残留暂存文件
    try:
        d = os.path.dirname(unc_path)
        for name in os.listdir(d):
            if name.startswith(os.path.basename(unc_path) + ".rev21_tmp"):
                try:
                    os.remove(os.path.join(d, name))
                except Exception:
                    pass
    except Exception:
        pass
    with io.open(stage, "wb") as f:
        f.write(patched.encode("utf-8"))
    # rename 覆盖(UNC 已存在文件不可直写,只能同卷 rename;SMB 瞬时锁重试穿透)
    last_err = None
    for _attempt in range(12):
        try:
            os.replace(stage, unc_path)
            last_err = None
            break
        except PermissionError as e:
            last_err = e
            import time as _t
            _t.sleep(1.0)
    if last_err is not None:
        raise last_err
    # 落盘校验:SMB 客户端有缓存,rename 后立即读可能拿到旧内容 -> 延时重读重试
    import time as _t
    import hashlib as _h
    want = _h.md5(patched.encode("utf-8")).hexdigest()
    ok = False
    for _attempt in range(6):
        _t.sleep(2.0)
        try:
            with io.open(unc_path, "rb") as f:
                on_disk = f.read()
            if _h.md5(on_disk).hexdigest() == want:
                ok = True
                break
        except Exception:
            pass
    assert ok, f"{unc_path} 落盘校验失败(重试 6 次仍不一致)"
    # 同步本地镜像
    mirror_path = os.path.join(MIRROR, rel.replace("/", "\\"))
    with io.open(mirror_path, "wb") as f:
        f.write(patched.encode("utf-8"))
    print(f"[DELIVER] {rel}: UNC 已覆盖并逐字节校验, 镜像已同步 ({len(on_disk)} bytes)")


def main():
    # account_manager.py: 基线用本地镜像(含 rev20;UNC 磁盘版被宿主侧回退成
    # 不含 rev20 的 CRLF 旧版,直接以 UNC 为基线会丢 rev20 的 login_at 补丁)
    with io.open(os.path.join(MIRROR, AM_REL.replace("/", "\\")), encoding="utf-8") as f:
        am = f.read()
    # 投递前备份 UNC 现状(防回退版里有镜像没有的改动)
    unc_am_path = os.path.join(UNC, AM_REL.replace("/", "\\"))
    with io.open(unc_am_path, "rb") as f:
        unc_now = f.read()
    import time as _time
    backup = os.path.join(MIRROR, ".unc_probe", f"account_manager.py.unc_backup_{int(_time.time())}")
    with io.open(backup, "wb") as f:
        f.write(unc_now)
    print(f"[BACKUP] UNC 现状已备份: {backup} ({len(unc_now)} bytes)")
    am_patched = patch_account_manager(am)
    # server.py: UNC 与镜像字节一致,任选其一;以 UNC 为源保持母本权威
    unc_sv = os.path.join(UNC, SV_REL.replace("/", "\\"))
    with io.open(unc_sv, encoding="utf-8") as f:
        sv = f.read()
    sv_patched = patch_server(sv)

    # 自校验:关键新符号必须存在
    for sym in ("_load_run_intent", "_save_run_intent", "restore_running_accounts",
                "_run_intent.add(account_id)", "_run_intent.discard(account_id)"):
        assert sym in am_patched, f"自校验失败: account_manager 缺 {sym}"
    assert "_auto_restore_loop" in sv_patched, "自校验失败: server 缺 _auto_restore_loop"
    assert "rev21" in am_patched and "rev21" in sv_patched, "自校验失败: 版本标记缺失"
    print("[SELFCHECK] 补丁内容断言全部通过")

    deliver(AM_REL, am_patched)
    deliver(SV_REL, sv_patched)
    print("[DONE] rev21 补丁投递完成")


if __name__ == "__main__":
    sys.exit(main())
