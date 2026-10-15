# -*- coding: utf-8 -*-
"""rev20: 账号卡片 24h 登录态倒计时 —— 一次性补丁脚本(母本=UNC \\Sphgj\sphgj)

四处改动:
  A. backend/login_capture.py  finalize_with_id 三条落盘路径均写 login_at
     (relogin / _log_finder_id 排重 / 新建),覆盖手动+自动 relogin 全路径
  B. backend/account_manager.py  _account_status 透出 login_at
  C. frontend/src/lib/api.ts     AccountStatus 增加 login_at
  D. frontend/src/pages/AccountsPage.tsx  卡片标题行加 LoginCountdown 徽标
     (每秒 tick;>6h 绿 / 1-6h 黄 / <1h 红 / 过期红字;无 login_at 显示"计时未知")

改完同步到本地镜像 D:\\SPHGJ(保持两边一致)。
"""
import io
import os
import shutil
import sys

UNC = r"\\Sphgj\sphgj"
LOCAL = r"D:\SPHGJ"

PATCHES = []


def patch(path, edits):
    PATCHES.append((path, edits))


# ---------------------------------------------------------------- A. login_capture.py
patch(UNC + r"\backend\login_capture.py", [
    # A1 import datetime
    ("import base64\nimport asyncio\n",
     "import base64\nimport asyncio\nimport datetime\n",
     "import datetime"),
    # A2 relogin 路径
    ('            acc["_wx_name"] = self.captured.get("wx_name") or acc.get("_wx_name", "")\n            self._save_config()',
     '            acc["_wx_name"] = self.captured.get("wx_name") or acc.get("_wx_name", "")\n'
     '            acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")\n'
     '            self._save_config()',
     "relogin login_at"),
    # A3 排重路径
    ('            dup["_wx_name"] = self.captured.get("wx_name") or dup.get("_wx_name", "")\n            dup["profile_dir"] = final_dir',
     '            dup["_wx_name"] = self.captured.get("wx_name") or dup.get("_wx_name", "")\n'
     '            dup["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")\n'
     '            dup["profile_dir"] = final_dir',
     "dedup login_at"),
    # A4 新建账号
    ('            "profile_dir": final_dir,\n            "auto_comment_enabled": False,',
     '            "profile_dir": final_dir,\n'
     '            "login_at": datetime.datetime.now().isoformat(timespec="seconds"),\n'
     '            "auto_comment_enabled": False,',
     "new-account login_at"),
])

# ---------------------------------------------------------------- B. account_manager.py
patch(UNC + r"\backend\account_manager.py", [
    ('            "has_aid": bool(acc.get("_aid")),\n            "wx_name": acc.get("_wx_name", ""),',
     '            "has_aid": bool(acc.get("_aid")),\n            "wx_name": acc.get("_wx_name", ""),\n'
     '            "login_at": acc.get("login_at"),',
     "status login_at"),
])

# ---------------------------------------------------------------- C. api.ts
patch(UNC + r"\frontend\src\lib\api.ts", [
    ("  wx_name: string\n  locked: boolean",
     "  wx_name: string\n  login_at: string | null\n  locked: boolean",
     "AccountStatus.login_at"),
])

# ---------------------------------------------------------------- D. AccountsPage.tsx
COUNTDOWN_TSX = '''const LOGIN_TTL_MS = 24 * 60 * 60 * 1000

function fmtRemain(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`
}

/** 登录态 24h 超时倒计时。锚点 = 登录成功时间 login_at(backend/login_capture.py 落盘),
 *  覆盖手动登录 / 重新登录 / 自动 relogin 三条路径。 */
function LoginCountdown({ loginAt }: { loginAt: string | null }) {
  const [, forceTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => forceTick((v) => v + 1), 1000)
    return () => clearInterval(timer)
  }, [])

  const unknown = (
    <Badge variant="outline" className="gap-1 text-muted-foreground" title="登录时间未知(旧账号数据),重新扫码后开始计时">
      <Clock className="h-3 w-3" />计时未知
    </Badge>
  )
  if (!loginAt) return unknown
  const loginTs = new Date(loginAt).getTime()
  if (Number.isNaN(loginTs)) return unknown

  const remain = loginTs + LOGIN_TTL_MS - Date.now()
  if (remain <= 0) {
    return (
      <Badge variant="outline" className="gap-1 border-destructive/50 text-destructive" title="登录态已超时(24 小时),请重新扫码">
        <Clock className="h-3 w-3" />登录态已过期
      </Badge>
    )
  }
  const cls = remain < 60 * 60 * 1000
    ? "border-destructive/50 text-destructive"
    : remain < 6 * 60 * 60 * 1000
      ? "border-amber-500/40 text-amber-600"
      : "border-emerald-500/40 text-emerald-600"
  return (
    <Badge variant="outline" className={`gap-1 ${cls}`} title={`登录于 ${fmtTime(loginAt)} · 24 小时后超时,请提前重新扫码`}>
      <Clock className="h-3 w-3" />登录态 {fmtRemain(remain)}
    </Badge>
  )
}

'''
patch(UNC + r"\frontend\src\pages\AccountsPage.tsx", [
    ('import { useState } from "react"',
     'import { useState, useEffect } from "react"',
     "useEffect import"),
    ('ChevronRight, Lock, Unlock } from "lucide-react"',
     'ChevronRight, Lock, Unlock, Clock } from "lucide-react"',
     "Clock import"),
    ('''              {acc.locked && (
                <Badge variant="outline" className="gap-1 border-amber-500/40 text-amber-600" title={`已锁定微信:${acc.locked_name}`}>
                  <Lock className="h-3 w-3" />已锁定
                </Badge>
              )}
''',
     '''              {acc.locked && (
                <Badge variant="outline" className="gap-1 border-amber-500/40 text-amber-600" title={`已锁定微信:${acc.locked_name}`}>
                  <Lock className="h-3 w-3" />已锁定
                </Badge>
              )}
              <LoginCountdown loginAt={acc.login_at} />
''',
     "countdown badge"),
    ("function Stat({",
     COUNTDOWN_TSX + "function Stat({",
     "LoginCountdown component"),
])

# ---------------------------------------------------------------- 执行
fail = False
for path, edits in PATCHES:
    with io.open(path, encoding="utf-8") as f:
        s = f.read()
    for old, new, tag in edits:
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {os.path.basename(path)} :: {tag} 命中 {n} 次(要求 1)")
            fail = True
            continue
        s = s.replace(old, new, 1)
    if fail:
        continue
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print(f"[OK] 已写入 {path}")

if fail:
    sys.exit("有补丁未命中,已中止(未写入任何文件由 continue 保证——除已写文件外)")

# ---------------------------------------------------------------- 自校验
CHECKS = [
    (UNC + r"\backend\login_capture.py", [
        "import datetime",
        'acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")',
        'dup["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")',
        '"login_at": datetime.datetime.now().isoformat(timespec="seconds"),',
    ]),
    (UNC + r"\backend\account_manager.py", ['"login_at": acc.get("login_at"),']),
    (UNC + r"\frontend\src\lib\api.ts", ["login_at: string | null"]),
    (UNC + r"\frontend\src\pages\AccountsPage.tsx", [
        "LoginCountdown", "LOGIN_TTL_MS", "<Clock className", "登录态已过期", "计时未知",
    ]),
]
ok = True
for path, keys in CHECKS:
    with io.open(path, encoding="utf-8") as f:
        s = f.read()
    for k in keys:
        if k not in s:
            print(f"[SELFCHECK FAIL] {os.path.basename(path)} 缺少: {k}")
            ok = False
print("SELFCHECK " + ("PASS" if ok else "FAIL"))
if not ok:
    sys.exit(1)

# ---------------------------------------------------------------- 同步本地镜像
MIRROR = [
    r"\backend\login_capture.py",
    r"\backend\account_manager.py",
    r"\frontend\src\lib\api.ts",
    r"\frontend\src\pages\AccountsPage.tsx",
]
for rel in MIRROR:
    src, dst = UNC + rel, LOCAL + rel
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    with io.open(src, encoding="utf-8") as f1, io.open(dst, encoding="utf-8") as f2:
        assert f1.read() == f2.read(), rel
    print(f"[MIRROR] {rel} -> 本地已同步且一致")
print("ALL DONE")
