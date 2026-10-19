# -*- coding: utf-8 -*-
"""rev22 补丁:扫码保存提速 + 退出登录态 + 评论模块合并。

按项目铁律一次性打补丁 + 投递母本:
  1) 每个改动点 assert 唯一命中(命中 0/多次立即报错退出,绝不静默跳过);
  2) 写本地镜像(D:\\SPHGJ) -> 写 UNC 同卷暂存 -> os.replace 覆盖 -> 延时重读校验;
  3) 末尾 SELFCHECK:逐条把「新代码必须在、旧代码必须不在」断言一遍。
不改 config.json / 不重启(重启由调用方单独做)。
"""
import io
import os
import sys
import time
import uuid

LOCAL = r"D:\SPHGJ"
UNC = r"\\Sphgj\sphgj"


# ---------------------------------------------------------------- 补丁定义
AM = "backend/account_manager.py"
SV = "backend/server.py"
LC = "backend/login_capture.py"
API = "frontend/src/lib/api.ts"
HOOK = "frontend/src/hooks/useAccounts.ts"
ACC_PAGE = "frontend/src/pages/AccountsPage.tsx"
SHELL = "frontend/src/components/layout/AppShell.tsx"
ADD_DLG = "frontend/src/components/accounts/AddAccountDialog.tsx"

PATCHES = {
    # ============ 后端 1:退出登录态 ============
    AM: [
        (
            "from .login_capture import LoginSession\n",
            "from .login_capture import LoginSession, clear_login_state\n",
            "AM-import-clear-login",
        ),
        (
            """    async def stop_account(self, account_id):
        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)
        self._run_intent.discard(account_id)
        self._save_run_intent()
        w = self.workers.pop(account_id, None)
        if w:
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())
""",
            """    async def stop_account(self, account_id):
        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)
        self._run_intent.discard(account_id)
        self._save_run_intent()
        w = self.workers.pop(account_id, None)
        if w:
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())

    async def logout_account(self, account_id):
        \"\"\"手动退出登录态(rev22):停 worker / 关 headed 浏览器 -> 清 profile 内登录文件。

        与「删除账号」的区别:账号配置(名称/ID/profile_dir)全部保留,只清登录 Cookie
        与 LocalStorage,下次必须重新扫码。_aid / _log_finder_id / locked_* 一律保留 ——
        重新扫码仍能命中同一账号,锁定校验也照旧生效。
        返回 (ok, 中文说明)。
        \"\"\"
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False, "账号不存在"
        # 1) 先停 worker 释放 profile 目录锁,否则清登录态必然失败
        if account_id in self.workers:
            try:
                await self.stop_account(account_id)
            except Exception as e:
                logger.warning(f"[{account_id}] 退出登录前停止 worker 失败: {e}")
        # 2) 关掉用户手动打开的 headed 浏览器(否则同样占着 profile)
        ctx = self._open_browsers.pop(account_id, None)
        if ctx:
            try:
                await close_context_safely(ctx, acc.get("profile_dir"), f"[{account_id}] logout")
            except Exception as e:
                logger.debug(f"[{account_id}] 退出登录时关闭 headed 浏览器异常(忽略): {e}")
        # 3) 清 profile 内登录文件(函数自带 ./profiles 范围校验),稍等句柄释放
        await asyncio.sleep(0.3)
        ok, msg = clear_login_state(acc.get("profile_dir") or "")
        # 4) 清登录时间(倒计时归零),另记退出时间供前端展示「未登录」
        acc["login_at"] = None
        acc["logout_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_config()
        await self._emit("accounts_updated", {"reason": "logout", "account_id": account_id})
        logger.info(f"[{account_id}] 已退出登录态: {msg}")
        return ok, msg
""",
            "AM-logout-method",
        ),
        (
            '            "login_at": acc.get("login_at"),\n',
            '            "login_at": acc.get("login_at"),\n            "logout_at": acc.get("logout_at"),\n',
            "AM-status-logout-at",
        ),
    ],
    # ============ 后端 2:logout 路由 ============
    SV: [
        (
            """@app.post("/api/accounts/{account_id}/start")
""",
            """@app.post("/api/accounts/{account_id}/logout")
async def logout_account(account_id: str):
    \"\"\"手动退出登录态(rev22):清该账号 profile 内的登录文件,账号配置保留。

    清理失败(浏览器没退干净)不抛 500,如实返回原因,前端据此提示重试 ——
    这是用户主动操作,失败原因必须可见,不能只留个红叉。
    \"\"\"
    ok, msg = await manager.logout_account(account_id)
    if msg == "账号不存在":
        raise HTTPException(404, msg)
    return {"ok": ok, "message": msg, "accounts": manager.status_snapshot()["accounts"]}


@app.post("/api/accounts/{account_id}/start")
""",
            "SV-logout-route",
        ),
    ],
    # ============ 后端 3:扫码保存提速(去掉固定 2.5s 空等) ============
    LC: [
        (
            """        try:
            await self.page.wait_for_timeout(2500)
        except Exception as e:
            logger.debug(f"[login:{self.sid}] 等待页面稳定异常(忽略): {e}")
""",
            """        # rev22 提速:这里原来是固定空等 2.5s。扫码后页面通常已就绪(上一段
        # localStorage 都读到了),空等纯属浪费 —— 实测「扫码成功 -> 抓取完成」
        # 的 5s 里约一半耗在这段死等。改成短等待,页面没就绪时由
        # _fetch_finder_identity 的 3 次重试(每次间隔 1s)兜底,不会更不可靠。
        try:
            await self.page.wait_for_timeout(600)
        except Exception as e:
            logger.debug(f"[login:{self.sid}] 等待页面稳定异常(忽略): {e}")
""",
            "LC-cut-fixed-wait",
        ),
    ],
    # ============ 前端 1:类型 + API ============
    API: [
        (
            "  login_at: string | null\n  locked: boolean\n",
            "  login_at: string | null\n  /** 手动退出登录态的时间(rev22);未退出过为 undefined */\n  logout_at?: string | null\n  locked: boolean\n",
            "API-AccountStatus-logout-at",
        ),
        (
            """  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
""",
            """  /** 退出登录态:清该账号 profile 内登录文件,账号保留(下次需重新扫码) */
  logoutAccount: (id: string) =>
    req<{ ok: boolean; message: string; accounts: AccountStatus[] }>(`/accounts/${id}/logout`, { method: "POST" }),
  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
""",
            "API-logoutAccount",
        ),
    ],
    # ============ 前端 2:hook 动作 ============
    HOOK: [
        (
            """    // 锁定/解锁微信身份:锁定后该账号只接受锁定的微信登录
    setLock: (id: string, locked: boolean) => act(id, (i) => (locked ? api.lockAccount(i) : api.unlockAccount(i))),
""",
            """    // 锁定/解锁微信身份:锁定后该账号只接受锁定的微信登录
    setLock: (id: string, locked: boolean) => act(id, (i) => (locked ? api.lockAccount(i) : api.unlockAccount(i))),
    // 退出登录态:清该账号登录文件(账号保留),之后需重新扫码
    logout: (id: string) => act(id, api.logoutAccount),
""",
            "HOOK-logout-action",
        ),
    ],
    # ============ 前端 3:账号卡片退出登录按钮 ============
    ACC_PAGE: [
        (
            "import { Plus, Play, Square, Trash2, Save, Loader2, MessageSquarePlus, Users, ExternalLink, ChevronRight, Lock, Unlock, Clock } from \"lucide-react\"",
            "import { Plus, Play, Square, Trash2, Save, Loader2, MessageSquarePlus, Users, ExternalLink, ChevronRight, Lock, Unlock, Clock, LogOut } from \"lucide-react\"",
            "ACC-icon-import",
        ),
        (
            "  const { busy, start, stop, openBrowser, remove, setLock } = useAccountActions(onRefresh)",
            "  const { busy, start, stop, openBrowser, remove, setLock, logout } = useAccountActions(onRefresh)",
            "ACC-destructure-logout",
        ),
        (
            """  const handleOpen = async () => {
""",
            """  // 退出登录态:清掉该账号的登录文件(账号卡片保留),之后需重新扫码
  const handleLogout = async () => {
    if (!window.confirm(`退出「${acc.name}」的登录态?\\n\\n会清除该账号的登录信息(账号本身保留),之后需要重新扫码登录。`)) return
    try {
      const r = await logout(acc.id)
      if (r?.ok) toast.success(r.message || "已退出登录态,请重新扫码")
      else toast.error("退出未完成:" + (r?.message || "登录文件未清干净,请重试"))
    } catch (e) {
      toast.error("操作失败:" + (e as Error).message)
    }
  }

  const handleOpen = async () => {
""",
            "ACC-handle-logout",
        ),
        (
            "              <LoginCountdown loginAt={acc.login_at} />",
            "              <LoginCountdown loginAt={acc.login_at} logoutAt={acc.logout_at} />",
            "ACC-countdown-prop",
        ),
        (
            """              {acc.locked ? <Lock className="h-4 w-4 text-amber-600" /> : <Unlock className="h-4 w-4" />}
            </Button>
""",
            """              {acc.locked ? <Lock className="h-4 w-4 text-amber-600" /> : <Unlock className="h-4 w-4" />}
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={handleLogout}
              disabled={isBusy || (!acc.logged_in && !acc.login_at)}
              title="退出登录态(清除登录信息,账号保留,之后需重新扫码)"
              aria-label="退出登录态"
            >
              {isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogOut className="h-4 w-4" />}
            </Button>
""",
            "ACC-logout-button",
        ),
        (
            """function LoginCountdown({ loginAt }: { loginAt: string | null }) {""",
            """function LoginCountdown({ loginAt, logoutAt }: { loginAt: string | null; logoutAt?: string | null }) {""",
            "ACC-countdown-signature",
        ),
        (
            """  const unknown = (
    <Badge variant="outline" className="gap-1 text-muted-foreground" title="登录时间未知(旧账号数据),重新扫码后开始计时">
      <Clock className="h-3 w-3" />计时未知
    </Badge>
  )
  if (!loginAt) return unknown""",
            """  // 手动退出过登录态:明确显示「未登录」,而不是含糊的「计时未知」
  if (!loginAt && logoutAt) {
    return (
      <Badge variant="outline" className="gap-1 text-muted-foreground" title={`已于 ${fmtTime(logoutAt)} 退出登录态,重新扫码后开始计时`}>
        <Clock className="h-3 w-3" />未登录
      </Badge>
    )
  }
  const unknown = (
    <Badge variant="outline" className="gap-1 text-muted-foreground" title="登录时间未知(旧账号数据),重新扫码后开始计时">
      <Clock className="h-3 w-3" />计时未知
    </Badge>
  )
  if (!loginAt) return unknown""",
            "ACC-countdown-logout-branch",
        ),
    ],
    # ============ 前端 4:导航合并(评论 -> 评论管理) ============
    SHELL: [
        (
            """import {
  LayoutDashboard, Users, MessageSquare, Reply, Moon, Sun,
  Play, Square, RefreshCw, Loader2, Trash2, Monitor, Type, Clapperboard,
} from "lucide-react\"""",
            """import {
  LayoutDashboard, Users, MessageSquare, Moon, Sun,
  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard,
} from "lucide-react\"""",
            "SHELL-icon-import",
        ),
        (
            """const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/accounts", label: "账号管理", icon: Users },
  { to: "/comments", label: "评论", icon: MessageSquare },
  { to: "/auto-reply", label: "自动回复", icon: Reply },
  { to: "/auto-delete", label: "自动删除", icon: Trash2 },
  { to: "/posts", label: "作品管理", icon: Clapperboard },
  { to: "/live-screen", label: "直播大屏", icon: Monitor },
]""",
            """// rev22:「自动回复」「自动删除」并入「评论管理」,不再单列导航项
const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/accounts", label: "账号管理", icon: Users },
  { to: "/comments", label: "评论管理", icon: MessageSquare },
  { to: "/posts", label: "作品管理", icon: Clapperboard },
  { to: "/live-screen", label: "直播大屏", icon: Monitor },
]""",
            "SHELL-nav-merge",
        ),
        (
            """const TITLES: Record<string, string> = { "/": "仪表盘", "/accounts": "账号管理", "/comments": "评论", "/auto-reply": "自动回复设置", "/auto-delete": "自动删除", "/live-screen": "直播大屏", "/posts": "作品管理" }""",
            """const TITLES: Record<string, string> = { "/": "仪表盘", "/accounts": "账号管理", "/comments": "评论管理", "/auto-reply": "评论管理", "/auto-delete": "评论管理", "/live-screen": "直播大屏", "/posts": "作品管理" }""",
            "SHELL-titles",
        ),
        (
            """            <p>多账号 · 增量抓取 · 自动回复</p>""",
            """            <p>多账号 · 增量抓取 · 评论管理</p>""",
            "SHELL-sidebar-hint",
        ),
    ],
    # ============ 前端 5:保存提速(启动不再阻塞弹窗) ============
    ADD_DLG: [
        (
            """      const acc = r?.account as { _dedup_updated?: boolean; id?: string; name?: string } | undefined
      if (reloginAccountId) {
        await api.startAccount(reloginAccountId)
        toast.success("已重新登录并启动")
      } else if (acc?._dedup_updated && acc.id) {
        await api.startAccount(acc.id).catch(() => {})
        toast.success(`已更新账号${acc.name ? ` ${acc.name}` : ""} 并启动`)
      } else {
        toast.success("账号已添加")
      }
      onOpenChange(false)
      onDone()
""",
            """      const acc = r?.account as { _dedup_updated?: boolean; id?: string; name?: string } | undefined
      // rev22 提速:finalize 返回 = 已落盘,立刻关弹窗给反馈。
      // 启动账号要新开浏览器 + 校验登录态(实测 4s+),放后台跑,
      // 不让用户对着「正在保存」转圈;失败再单独提示。
      onOpenChange(false)
      onDone()
      const startId = reloginAccountId || (acc?._dedup_updated ? acc.id : undefined)
      if (!startId) { toast.success("账号已添加"); return }
      toast.success(reloginAccountId
        ? "已重新登录,正在启动…"
        : `已更新账号${acc?.name ? ` ${acc.name}` : ""},正在启动…`)
      void (async () => {
        try {
          await api.startAccount(startId)
          toast.success(reloginAccountId ? "已重新登录并启动" : "账号已启动")
        } catch (e) {
          toast.error("账号已保存,但启动失败:" + (e as Error).message)
        } finally {
          onDone()
        }
      })()
""",
            "ADD-finalize-nonblocking",
        ),
    ],
}

# 新文件(直接以内容投递):相对路径 -> 本地镜像路径
NEW_FILES = [
    "frontend/src/components/comments/CommentListPanel.tsx",
    "frontend/src/components/comments/AutoReplyPanel.tsx",
    "frontend/src/components/comments/AutoDeletePanel.tsx",
    "frontend/src/pages/CommentsPage.tsx",
    "frontend/src/pages/AutoReplyPage.tsx",
    "frontend/src/pages/AutoDeletePage.tsx",
]

# 自校验:补丁后必须出现 / 必须消失
SELFCHECK = {
    AM: [("logout_account", True), ("clear_login_state", True), ('"logout_at": acc.get("logout_at")', True)],
    SV: [("@app.post(\"/api/accounts/{account_id}/logout\")", True), ("manager.logout_account", True)],
    LC: [("wait_for_timeout(600)", True), ("wait_for_timeout(2500)", False)],
    API: [("logoutAccount", True), ("logout_at?: string | null", True)],
    HOOK: [("logout: (id: string)", True)],
    ACC_PAGE: [("handleLogout", True), ("logoutAt={acc.logout_at}", True), ("LogOut", True)],
    SHELL: [('label: "评论管理"', True), ('"/auto-reply", label', False), ("Reply,", False)],
    ADD_DLG: [("正在启动…", True), ("已重新登录并启动", True)],
}


def to_unc(rel):
    return os.path.join(UNC, rel.replace("/", os.sep))


def to_local(rel):
    return os.path.join(LOCAL, rel.replace("/", os.sep))


def deliver(rel, data: bytes):
    """写本地镜像 + UNC(同卷暂存 -> rename 覆盖 -> 延时重读校验)。

    UNC 共享对【已存在文件】只读,唯一通道是同卷 rename;暂存名必须唯一,
    否则会踩历史残留文件的僵尸锁(WinError 5)。
    """
    lp = to_local(rel)
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    with io.open(lp, "wb") as f:
        f.write(data)

    up = to_unc(rel)
    os.makedirs(os.path.dirname(up), exist_ok=True)

    # 宿主侧对 .py 有「扫描/短暂独占」窗口,实测可持续 20s 以上,且被拒过的暂存文件
    # 会变粘(后续重试一直 WinError 5)。策略:每 3 次失败就换一个全新暂存名重写。
    last = None
    stage = None
    for attempt in range(40):
        if attempt % 3 == 0:
            if stage:
                try:
                    os.remove(stage)
                except Exception:
                    pass
            stage = up + f".rev22_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            with io.open(stage, "wb") as f:
                f.write(data)
        try:
            os.replace(stage, up)
            last = None
            break
        except PermissionError as e:
            last = e
            time.sleep(1.5)
    if last is not None:
        raise last

    # SMB 客户端缓存:rename 后立刻读会读回旧内容,必须延时重读
    for i in range(6):
        time.sleep(1.0)
        with io.open(up, "rb") as f:
            on_disk = f.read()
        if on_disk == data:
            return
    raise AssertionError(f"{rel} 落盘校验失败(重读内容与写入不一致)")


def cleanup_stale_stages():
    """清掉历史投递遗留的暂存文件(我自己造的 .rev2x_*)。

    残留同名/同名段暂存文件会踩僵尸锁,让后续 rename 反复 WinError 5 ——
    rev21 已踩过一次,这里每次投递前先扫干净。
    """
    removed = 0
    for root, _dirs, files in os.walk(UNC):
        if "\\.git" in root or "node_modules" in root or "profiles" in root:
            continue
        for n in files:
            if ".rev21_tmp" in n or ".rev22_" in n or ".rev23_" in n:
                try:
                    os.remove(os.path.join(root, n))
                    removed += 1
                except Exception:
                    pass
    if removed:
        print(f"[CLEAN] 清理 {removed} 个历史暂存文件")


def main():
    cleanup_stale_stages()

    # ---- 1) 打补丁(读 UNC 原文,唯一命中才替换)----
    patched = {}
    for rel, items in PATCHES.items():
        with io.open(to_unc(rel), "rb") as f:
            src = f.read().decode("utf-8")
        for old, new, tag in items:
            n = src.count(old)
            if n != 1:
                print(f"[FAIL] {rel} :: {tag} 命中 {n} 次(应为 1),中止")
                return 1
            src = src.replace(old, new, 1)
        patched[rel] = src
        print(f"[PATCH] {rel} 应用 {len(items)} 处")

    # ---- 2) 新文件取本地镜像内容(已用工具写好)----
    for rel in NEW_FILES:
        lp = to_local(rel)
        if not os.path.exists(lp):
            print(f"[FAIL] 新文件本地镜像不存在: {lp}")
            return 1
        with io.open(lp, "rb") as f:
            patched[rel] = f.read().decode("utf-8")
        print(f"[NEW]   {rel} ({len(patched[rel].encode('utf-8'))}B)")

    # ---- 3) 投递 ----
    for rel, text in patched.items():
        deliver(rel, text.encode("utf-8"))
        print(f"[DELIVER] {rel} ok ({len(text.encode('utf-8'))}B)")

    # ---- 4) 自校验(从 UNC 重读,不信内存)----
    print("\n==== SELFCHECK ====")
    bad = 0
    for rel, checks in SELFCHECK.items():
        with io.open(to_unc(rel), "rb") as f:
            s = f.read().decode("utf-8")
        for token, want in checks:
            got = token in s
            ok = (got == want)
            if not ok:
                bad += 1
            print(f"  {'PASS' if ok else 'FAIL'}  {rel:52s} {'含' if want else '不含'} {token!r}")
    print("SELFCHECK", "PASS" if bad == 0 else f"FAIL({bad})")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
