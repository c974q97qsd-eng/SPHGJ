# -*- coding: utf-8 -*-
"""rev28: 孤儿 Chromium 进程锁死 profile 的自愈修复。

事故(2026-10-20 免税仓 uspoloassn2):扫码 4 次全部成功(auth_data 抓到
finder/微信名、config 里 _aid/login_at 都写对了),但每次 10 秒后 worker 打开
评论页又回 login.html,账号永远 running=false。根因是 context.close() 没抛错
却留下孤儿 Chromium 进程,独占 profile 里的 Cookies —— 之后所有扫码写进去的
登录态都落不了盘。

四处加固:
  a) browser.reap_profile_orphans(): 按 --user-data-dir 精确匹配 kill 孤儿
  b) login_capture.clear_login_state(): 有残留先 kill 再清一遍(以前直接返回失败)
  c) relogin 复用 profile 前 / 关 context 后 都清一次
  d) AccountManager.start_account 前清孤儿;手动扫码成功后自动拉起账号
  e) 新增 POST /api/system/reap-orphans 运维入口
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BROWSER = os.path.join(ROOT, "backend", "browser.py")
LOGIN = os.path.join(ROOT, "backend", "login_capture.py")
MGR = os.path.join(ROOT, "backend", "account_manager.py")
SERVER = os.path.join(ROOT, "backend", "server.py")

# ============================ browser.py ============================
REAP_FUNC = '''def reap_profile_orphans(profile_dir, log_tag=""):
    """按 --user-data-dir 精确匹配, kill 该 profile 的所有 Chromium 进程(rev28)。

    解决的问题:context.close() 不抛异常,但浏览器进程其实还活着(扫码窗口被手动
    关掉、TargetClosedError、管道异常等)。残留进程会独占 profile 里的
    Cookies / LocalStorage,此后同一 profile 再 launch 出来的实例既写不进也读不到
    新的登录态 —— 表现就是「扫码成功,一启动又回登录页」的死循环
    (2026-10-20 免税仓 uspoloassn2 连续 4 次扫码失败的真因)。

    安全性:Chromium 用 profile 锁保证同一 user_data_dir 同一时刻只有一个实例,
    所以匹配到的进程必然属于这个 profile;但调用方仍要自己确认此刻没有本进程的
    合法 context 正在使用它(见 AccountManager._profile_busy)。

    返回被 kill 的 pid 列表(没装 psutil 时返回空列表,不抛错)。
    """
    if not profile_dir:
        return []
    try:
        import psutil
    except ImportError:
        logger.warning(f"[browser] {log_tag} 未装 psutil,无法清理孤儿 chrome 进程")
        return []
    abs_dir = os.path.abspath(profile_dir)
    killed = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            for arg in (p.info.get("cmdline") or []):
                if arg.startswith("--user-data-dir="):
                    if os.path.abspath(arg.split("=", 1)[1]) == abs_dir:
                        pid = p.info["pid"]
                        p.kill()
                        killed.append(pid)
                        break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    if killed:
        more = "…" if len(killed) > 8 else ""
        logger.info(f"[browser] {log_tag} 清理 {len(killed)} 个孤儿 chrome 进程 "
                    f"pids={killed[:8]}{more} profile={profile_dir}")
    return killed


'''

OLD_CLOSE_KILL = '''    if closed_ok or not profile_dir:
        return
    try:
        import psutil
    except ImportError:
        logger.warning(f"[browser] {log_tag} context.close 失败且未装 psutil,无法兜底 kill 残留进程")
        return
    abs_dir = os.path.abspath(profile_dir)
    killed = 0
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            for arg in (p.info.get("cmdline") or []):
                if arg.startswith("--user-data-dir="):
                    if os.path.abspath(arg.split("=", 1)[1]) == abs_dir:
                        p.kill()
                        killed += 1
                        break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    if killed:
        logger.info(f"[browser] {log_tag} 兜底 kill {killed} 个残留 chrome 进程(profile={profile_dir})")'''

NEW_CLOSE_KILL = '''    if closed_ok or not profile_dir:
        return
    reap_profile_orphans(profile_dir, log_tag=f"{log_tag} 兜底")'''

# ========================= login_capture.py =========================
OLD_WIPE_BODY = '''    try:
        if os.path.isdir(target):
            for name in os.listdir(target):
                p = os.path.join(target, name)
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except Exception as e:
                    logger.debug(f"[lock-clean] 删除 {p} 失败: {e}")
        os.makedirs(target, exist_ok=True)
    except Exception as e:
        return False, f"清理登录态失败: {e}"
    leftover = os.listdir(target) if os.path.isdir(target) else ["<目录不存在>"]
    if leftover:
        return False, f"登录态未清干净(残留 {len(leftover)} 项,浏览器可能未完全退出)"
    return True, "已清除本账号登录态,登录环境已重置"'''

NEW_WIPE_BODY = '''    try:
        if os.path.isdir(target):
            _wipe_dir(target)
        os.makedirs(target, exist_ok=True)
    except Exception as e:
        return False, f"清理登录态失败: {e}"
    leftover = os.listdir(target) if os.path.isdir(target) else ["<目录不存在>"]
    if leftover:
        # rev28: 残留基本都是孤儿 Chromium 占着 Cookies / LocalStorage 删不掉(文件锁)。
        # 以前这里直接返回 false,调用方只提示「浏览器可能未完全退出」,脏 profile 就
        # 一直留着,之后每次扫码写进去的 cookie 都被它顶掉 —— 免税仓 2026-10-20 的
        # 死循环根因。现在先按 profile_dir kill 孤儿,等句柄释放后再清一遍。
        pids = reap_profile_orphans(target, log_tag="[lock-clean] 兜底")
        if pids:
            time.sleep(1.2)
            _wipe_dir(target)
            leftover = os.listdir(target) if os.path.isdir(target) else ["<目录不存在>"]
    if leftover:
        return False, f"登录态未清干净(残留 {len(leftover)} 项,浏览器可能未完全退出)"
    return True, "已清除本账号登录态,登录环境已重置"'''

WIPE_FUNC = '''def _wipe_dir(target):
    """清空目录内容(尽力而为,保留目录本身)。删不掉的项只记 debug,由调用方统计残留。"""
    for name in os.listdir(target):
        p = os.path.join(target, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except Exception as e:
            logger.debug(f"[lock-clean] 删除 {p} 失败: {e}")
    os.makedirs(target, exist_ok=True)


'''

OLD_START_PROFILE = '''        if self.account:
            # relogin:复用原账号 profile_dir(失效 cookie 会被扫码覆盖)
            self.profile_dir = self.account["profile_dir"]
        else:'''

NEW_START_PROFILE = '''        if self.account:
            # relogin:复用原账号 profile_dir(失效 cookie 会被扫码覆盖)
            self.profile_dir = self.account["profile_dir"]
            # rev28: 复用旧目录前先清孤儿。有残留进程独占该 profile 时,扫码写进去的
            # cookie 落不了盘,关掉就没了 —— 表现为「扫码成功但一启动又回登录页」。
            reap_profile_orphans(self.profile_dir, log_tag=f"[login:{self.sid}] 扫码前")
        else:'''

OLD_CLOSE_TAIL = '''        if self.context:
            await close_context_safely(self.context, self.profile_dir, f"[login:{self.sid}]")
            self.context = None
            self.page = None
        if not keep_profile and self.profile_dir and not self._finalized:'''

NEW_CLOSE_TAIL = '''        if self.context:
            await close_context_safely(self.context, self.profile_dir, f"[login:{self.sid}]")
            self.context = None
            self.page = None
        # rev28: context 关完再兜底清一次 —— ctx.close() 不抛错不代表进程真退了
        # (扫码窗口被手动关掉时最常见),孤儿会一直锁着 profile。
        if self.profile_dir:
            try:
                reap_profile_orphans(self.profile_dir, log_tag=f"[login:{self.sid}] 收尾")
            except Exception as e:
                logger.debug(f"[login:{self.sid}] 收尾清理孤儿失败: {e}")
        if not keep_profile and self.profile_dir and not self._finalized:'''

# ======================= account_manager.py ========================
MGR_HELPERS = '''    # ---------- rev28: 孤儿进程清理 ----------
    _LOGIN_TERMINAL = ("finalized", "success", "failed", "cancelled")

    def _has_active_login(self, account_id):
        """该账号是否有一个还没到终态的扫码会话(说明 profile 正被它占用)。"""
        for s in list((self.login_sessions or {}).values()):
            acc = getattr(s, "account", None) or {}
            if acc.get("id") == account_id and \\
                    getattr(s, "status", "") not in self._LOGIN_TERMINAL:
                return True
        return False

    def _profile_busy(self, account_id):
        """该账号 profile 是否正被已知的活会话占用:worker context / 扫码 / 远程串流。

        清孤儿是「按 profile_dir 全杀」,所以必须先确认没有自己的合法 context 在用,
        否则会把正在抓取或正在串流的浏览器一起杀掉。
        """
        w = self.workers.get(account_id)
        if w is not None and getattr(w, "context", None) is not None:
            return True
        if self._has_active_login(account_id):
            return True
        reg = getattr(self, "remote_registry", None)
        if reg is not None:
            try:
                if reg.get(account_id) is not None:
                    return True
            except Exception:
                pass
        return False

'''

OLD_ADD_ACCOUNT = '''    async def add_account(self, account, headless=True):
        worker = AccountWorker(account, self.storage, self.config, self._emit)'''

NEW_ADD_ACCOUNT = '''    async def add_account(self, account, headless=True):
        # rev28: launch 之前清掉锁死 profile 的孤儿 Chromium。有残留进程占着
        # Cookies / LocalStorage 时,新实例既写不进也读不到登录态,扫多少次码都白扫。
        if not self._profile_busy(account["id"]):
            try:
                reap_profile_orphans(account.get("profile_dir"),
                                     log_tag=f"[{account['id']}] 启动前")
            except Exception as e:
                logger.warning(f"[{account['id']}] 启动前清理孤儿进程失败: {e}")
        worker = AccountWorker(account, self.storage, self.config, self._emit)'''

OLD_START_RELOGIN = '''        if account_id in self.workers:
            await self.stop_account(account_id)
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit, account=acc,
                            auto_finalize=auto, on_finished=on_finished)'''

NEW_START_RELOGIN = '''        if account_id in self.workers:
            await self.stop_account(account_id)
        # rev28: 停掉 worker 后立刻清孤儿,给扫码一个真正干净的 profile
        try:
            reap_profile_orphans(acc.get("profile_dir"), log_tag=f"[{account_id}] 扫码前")
        except Exception as e:
            logger.warning(f"[{account_id}] 扫码前清理孤儿进程失败: {e}")
        if on_finished is None:
            # rev28: 手动扫码成功后自动把账号拉起来。以前手动模式不给回调,扫码到终态
            # 后没人 start_account,而上面已经把 worker 停掉了 —— 用户看到的是
            # 「扫码成功但账号仍然未登录」,还得自己再点一次启动。
            def on_finished(sid, status, aid):
                asyncio.create_task(self._on_relogin_finished(aid, status, sid))
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit, account=acc,
                            auto_finalize=auto, on_finished=on_finished)'''

OLD_RELOGIN_FINISHED = '''    async def _on_relogin_finished(self, account_id, status):
        if self._relogin_active_sid:
            self.login_sessions.pop(self._relogin_active_sid, None)
        if status == "finalized":
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"自动 relogin 后启动失败 {account_id}: {e}")
        self._relogin_active_acc = None
        self._relogin_active_sid = None
        await self._emit("relogin_queue", self._relogin_snapshot())
        await self._run_next_relogin()'''

NEW_RELOGIN_FINISHED = '''    async def _on_relogin_finished(self, account_id, status, sid=None):
        if sid:
            self.login_sessions.pop(sid, None)
        elif self._relogin_active_sid:
            self.login_sessions.pop(self._relogin_active_sid, None)
        if status == "finalized":
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"relogin 后启动失败 {account_id}: {e}")
        # rev28: 只有「当前活跃的那个自动 relogin」结束才推进队列。
        # 手动扫另一个号结束时也走这个回调,无条件清会把自动队列的状态抹掉。
        if self._relogin_active_acc == account_id:
            self._relogin_active_acc = None
            self._relogin_active_sid = None
            await self._emit("relogin_queue", self._relogin_snapshot())
            await self._run_next_relogin()'''

# ============================ server.py ============================
REAP_API = '''@app.post("/api/system/reap-orphans")
async def system_reap_orphans(account: str = Query("")):
    """清理锁死 profile 的孤儿 Chromium 进程(rev28)。

    不传 account 则遍历配置里的所有账号;正被占用(运行中 / 扫码中 / 串流中)
    的账号自动跳过,不会误杀。返回每个账号被 kill 的 pid 列表。
    """
    accs = [a for a in manager.config.get("accounts", [])
            if (not account or a.get("id") == account)]
    if account and not accs:
        raise HTTPException(404, "账号不存在")
    items = []
    for a in accs:
        aid = a.get("id") or ""
        if manager._profile_busy(aid):
            items.append({"account": aid, "skipped": "profile 正被占用(运行中/扫码中/串流中)"})
            continue
        pids = reap_profile_orphans(a.get("profile_dir"), log_tag=f"[reap:{aid}]")
        items.append({"account": aid, "profile_dir": a.get("profile_dir"),
                      "killed": pids})
    return {"items": items}


'''

PATCHES = [
    # ---- browser.py ----
    (BROWSER, 'async def close_context_safely(ctx, profile_dir=None, log_tag=""):',
     REAP_FUNC + 'async def close_context_safely(ctx, profile_dir=None, log_tag=""):',
     "browser: 新增 reap_profile_orphans"),
    (BROWSER, OLD_CLOSE_KILL, NEW_CLOSE_KILL, "browser: close 兜底改用 reap"),

    # ---- login_capture.py ----
    (LOGIN, 'import shutil\nimport base64', 'import shutil\nimport time\nimport base64',
     "login: import time"),
    (LOGIN, 'from .browser import launch_stealth, close_context_safely',
     'from .browser import launch_stealth, close_context_safely, reap_profile_orphans',
     "login: import reap_profile_orphans"),
    (LOGIN, 'def clear_login_state(profile_dir: str):',
     WIPE_FUNC + 'def clear_login_state(profile_dir: str):',
     "login: 新增 _wipe_dir"),
    (LOGIN, OLD_WIPE_BODY, NEW_WIPE_BODY, "login: 清登录态兜底 kill 重清"),
    (LOGIN, OLD_START_PROFILE, NEW_START_PROFILE, "login: 扫码前清孤儿"),
    (LOGIN, OLD_CLOSE_TAIL, NEW_CLOSE_TAIL, "login: 关 context 后收尾清孤儿"),

    # ---- account_manager.py ----
    (MGR, 'from .browser import launch_stealth, close_context_safely',
     'from .browser import launch_stealth, close_context_safely, reap_profile_orphans',
     "mgr: import reap_profile_orphans"),
    (MGR, '        self.human_stopped = set()\n',
     '        self.human_stopped = set()\n'
     '        # rev28: 由 server 注入,判断 profile 是否正被远程串流占用(清孤儿时避免误杀)\n'
     '        self.remote_registry = None\n',
     "mgr: remote_registry 字段"),
    (MGR, '    async def _ensure_playwright(self):',
     MGR_HELPERS + '    async def _ensure_playwright(self):',
     "mgr: _has_active_login / _profile_busy"),
    (MGR, OLD_ADD_ACCOUNT, NEW_ADD_ACCOUNT, "mgr: add_account 前清孤儿"),
    (MGR, OLD_START_RELOGIN, NEW_START_RELOGIN, "mgr: relogin 清孤儿 + 自动拉起"),
    (MGR, OLD_RELOGIN_FINISHED, NEW_RELOGIN_FINISHED, "mgr: relogin 终态带 sid + 队列守卫"),

    # ---- server.py ----
    (SERVER, 'from .remote_view import RemoteRegistry',
     'from .remote_view import RemoteRegistry\nfrom .browser import reap_profile_orphans',
     "server: import reap_profile_orphans"),
    (SERVER, 'remote_registry = RemoteRegistry(manager, config, storage)',
     'remote_registry = RemoteRegistry(manager, config, storage)\n'
     'manager.remote_registry = remote_registry  # rev28: 清孤儿时避免误杀正在串流的浏览器',
     "server: 注入 remote_registry"),
    (SERVER, '@app.post("/api/system/restart")',
     REAP_API + '@app.post("/api/system/restart")',
     "server: reap-orphans 接口"),
]


def main():
    by_file = {}
    for path, old, new, tag in PATCHES:
        by_file.setdefault(path, []).append((old, new, tag))

    for path, items in by_file.items():
        s = io.open(path, encoding="utf-8").read()
        for old, new, tag in items:
            n = s.count(old)
            assert n == 1, f"{tag}: 命中 {n} 次(应为 1) in {os.path.basename(path)}"
            s = s.replace(old, new, 1)
        io.open(path, "w", encoding="utf-8", newline="").write(s)
        print(f"[写入] {os.path.basename(path)}  {len(items)} 处")

    # ---------- 自校验 ----------
    checks = {
        BROWSER: ["def reap_profile_orphans(profile_dir, log_tag=\"\"):",
                  'reap_profile_orphans(profile_dir, log_tag=f"{log_tag} 兜底")'],
        LOGIN: ["import time", ", reap_profile_orphans",
                "def _wipe_dir(target):", "[lock-clean] 兜底",
                "扫码前", "收尾清理孤儿"],
        MGR: [", reap_profile_orphans", "self.remote_registry = None",
              "def _has_active_login", "def _profile_busy",
              "启动前清理孤儿进程失败", "def on_finished(sid, status, aid):",
              "async def _on_relogin_finished(self, account_id, status, sid=None):",
              'if self._relogin_active_acc == account_id:'],
        SERVER: ["from .browser import reap_profile_orphans",
                 "manager.remote_registry = remote_registry",
                 '@app.post("/api/system/reap-orphans")'],
    }
    ok = True
    for path, needles in checks.items():
        s = io.open(path, encoding="utf-8").read()
        for nd in needles:
            if nd not in s:
                print(f"[FAIL] {os.path.basename(path)} 缺少: {nd}")
                ok = False
    # 旧逻辑必须彻底消失
    gone = {BROWSER: ["killed += 1"], LOGIN: ["for name in os.listdir(target):\n                p ="], }
    for path, needles in gone.items():
        s = io.open(path, encoding="utf-8").read()
        for nd in needles:
            if nd in s:
                print(f"[FAIL] {os.path.basename(path)} 旧逻辑残留: {nd!r}")
                ok = False
    print("SELFCHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
