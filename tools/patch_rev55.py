# -*- coding: utf-8 -*-
"""rev55 补丁:把「掉线状态准确 -> 根本不发包」补齐三处。

用户 2026-10-28 的要求:
    「现在每轮会先试 HTTP，只要你掉线状态是准确的，掉线状态的账号就不用去发包了，
      直到扫码登录后才加入轮询任务。」

rev54 已经做了「运行中掉线 -> mark_offline -> _fetch_loop 只等事件不发包」,
但实测(启动 6 号)发现三处缺口:

缺口 1  启动时就未登录的账号只 w.stop(),【没有落盘】:
        - data/auth_state.json 一直是空的 -> _is_known_offline 形同虚设
        - 用户每点一次「启动」都要白开一次 Chromium(8~15s)才被告知要扫码
        - /api/accounts 里该账号永远 auth_state=unknown,看不出已知掉线
缺口 2  _run_fetch_round 里,已经用 auth_data 硬验判过「未登录」且 context 还开着时,
        仍然会先发一个 HTTP 包去撞 300333(为了凑证据链)。状态既然已经准确,
        这一枪就没必要打。
缺口 3  status_snapshot 对不在 workers 里的账号一律报 auth_state=unknown,
        已经落盘的 offline 事实被掩盖。

一次性补丁,每条替换都断言唯一命中;跑完自校验。
"""
import io
import sys

P = r"D:\SPHGJ\backend\account_manager.py"
s = io.open(P, encoding="utf-8").read()
orig = s

PATCHES = []

# ---------------------------------------------------------------- 缺口 1
PATCHES.append((
    """        else:
            # 未登录:关掉刚开的 context 释放进程(避免 chrome-headless-shell.exe 残留),
            # 不入 workers;前端据返回的 logged_in=false 自动转 relogin 扫码。
            await w.stop()""",
    """        else:
            # rev55: 启动时 auth_data 硬验就判「未登录」—— 这是确定性证据,必须记住。
            # 原来这里只 w.stop():虽然也不发任何请求,但状态没落盘,于是
            #   ① 用户每点一次「启动」都要白开一次 Chromium(8~15s)才被告知要扫码;
            #   ② data/auth_state.json 永远是空的,_is_known_offline() 形同虚设;
            #   ③ /api/accounts 里该账号永远 auth_state=unknown。
            # 这里只做「记账 + 落盘」,【不】走 mark_offline 的自动排队 ——
            # 前端拿到 logged_in=false 会自己弹扫码,别让同一账号被两个扫码会话同时拉起。
            try:
                await self._remember_offline(account_id, acc,
                                             "启动时 auth_data 硬验未通过(未登录)")
            except Exception as e:
                logger.debug(f"[{account_id}] 启动未登录记账异常(忽略): {e}")
            # 未登录:关掉刚开的 context 释放进程(避免 chrome-headless-shell.exe 残留),
            # 不入 workers;前端据返回的 logged_in=false 自动转 relogin 扫码。
            await w.stop()""",
    "缺口1 启动未登录也要落盘",
))

# 新增 _remember_offline(放在 _auth_offline_on_start 之前)
PATCHES.append((
    """    async def _auth_offline_on_start(self, account_id, acc):""",
    """    async def _remember_offline(self, account_id, acc, reason):
        \"\"\"rev55: 只记账(落盘 + 通知前端),不排队扫码。

        用于「启动时就发现未登录」这条路径:扫码由前端 logged_in=false 触发,
        后台再排一个自动扫码会话会和它抢同一个 profile。
        \"\"\"
        self._auth_offline[account_id] = {
            "reason": reason or "",
            "ts": time.time(),
            "fp": auth_fingerprint(acc) if acc else "",
        }
        self._save_auth_offline()
        try:
            await self._emit("engine_status", self.status_snapshot())
        except Exception:
            pass

    async def _auth_offline_on_start(self, account_id, acc):""",
    "缺口1 新增 _remember_offline",
))

# ---------------------------------------------------------------- 缺口 2
PATCHES.append((
    """        if self.auth_state == "offline":
            logger.info(f"[{self.account['id']}] 登录已失效(待扫码),本轮不发任何请求")
            return None, False""",
    """        if self.auth_state == "offline":
            logger.info(f"[{self.account['id']}] 登录已失效(待扫码),本轮不发任何请求")
            return None, False
        # rev55: 状态已经准确的就别再打这一枪。
        # context 还开着 + 刚做过 auth_data 硬验 + 硬验结论是「没登录身份」——
        # 这就是最确定的掉线证据,不必再发一个 HTTP 包去撞一次 300333 才肯判。
        # (context 为 None 时不走这条:那说明 _verify_result 是上一轮的残留,不作数)
        if self.context is not None and self._verify_result == "no" and not self.logged_in:
            await self.mark_offline("auth_data 硬验未通过(已有明确结论),本轮不发请求")
            return None, False""",
    "缺口2 已判未登录不再发包",
))

# ---------------------------------------------------------------- 缺口 3
PATCHES.append((
    """            "auth_state": getattr(w, "auth_state", "unknown") if w else "unknown",
            "auth_reason": getattr(w, "auth_reason", "") if w else "",""",
    """            "auth_state": self._auth_state_view(account_id, w),
            "auth_reason": (getattr(w, "auth_reason", "") if w
                            else ((self._auth_offline.get(account_id) or {}).get("reason") or "")),""",
    "缺口3 快照回落已落盘的离线状态",
))

PATCHES.append((
    """    @staticmethod
    def _probe_view(w):""",
    """    def _auth_state_view(self, account_id, w):
        \"\"\"rev55: 在网状态视图。worker 不在时说真话 —— 已经落盘判过掉线的就报 offline。\"\"\"
        if w is not None:
            st = getattr(w, "auth_state", "") or "unknown"
            if st != "unknown":
                return st
        if account_id in self._auth_offline:
            return "offline"
        return "unknown"

    @staticmethod
    def _probe_view(w):""",
    "缺口3 新增 _auth_state_view",
))

for i, (old, new, tag) in enumerate(PATCHES, 1):
    cnt = s.count(old)
    if cnt == 0:
        if s.count(new) >= 1:
            print(f"  [skip] {tag} 已打过")
            continue
        print(f"  [FAIL] #{i} {tag}: 原文未命中(0 次)")
        sys.exit(1)
    if cnt != 1:
        print(f"  [FAIL] #{i} {tag}: 命中 {cnt} 次,不唯一")
        sys.exit(1)
    s = s.replace(old, new, 1)
    print(f"  [ok] #{i} {tag}")

if s != orig:
    io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------- 自校验 ----------------
print("---- 自校验")
checks = [
    ("启动未登录记账调用", "await self._remember_offline(account_id, acc," in s),
    ("_remember_offline 定义", "async def _remember_offline(self, account_id, acc, reason):" in s),
    ("已判未登录不再发包", 'self._verify_result == "no" and not self.logged_in' in s),
    ("快照状态视图", '"auth_state": self._auth_state_view(account_id, w),' in s),
    ("_auth_state_view 定义", "def _auth_state_view(self, account_id, w):" in s),
    ("旧的裸 w.stop 分支已替换", """            await w.stop()""" in s),
    ("未把 mark_offline 塞进启动分支",
     'await self._remember_offline(account_id, acc,' in s
     and 'await w.mark_offline("启动时' not in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {n}")
print("语法检查:", end=" ")
try:
    compile(s, P, "exec")
    print("OK")
except SyntaxError as e:
    print("FAIL", e)
    sys.exit(1)
if bad:
    print("SELFCHECK FAIL:", bad)
    sys.exit(1)
print("SELFCHECK PASS")
