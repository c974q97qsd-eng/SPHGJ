# -*- coding: utf-8 -*-
"""rev20: 账号卡片 24h 登录态倒计时 —— 补丁 + 远程投递。

通道说明(实测):
- UNC 共享对「已存在文件」只读(属主是主机侧身份),但允许「新建文件/目录」;
- 同卷 os.replace(暂存文件 -> 目标) 可覆盖已存在文件(目录 Modify 权限含
  FILE_DELETE_CHILD),这是唯一可用的远程写通道;
- 投递后文件属主变为本 SMB 会话身份,主机程序只读不受影响。

流程: 读 UNC 原文 -> 内存打补丁(断言唯一命中) -> 写本地 D:\\SPHGJ 镜像
(供本地构建) -> UNC 同卷暂存 -> rename 覆盖 -> 逐字节校验。
"""
import io
import os
import shutil
import sys

UNC = r"\\Sphgj\sphgj"
LOCAL = r"D:\SPHGJ"
TAG = "rev20"

# ---------------------------------------------------------------- 补丁定义
# (基于 UNC 当前母本内容;锚点均已在只读预览中断言命中)
PATCHES = {
    r"\backend\login_capture.py": [
        ("import base64\nimport asyncio\n",
         "import base64\nimport asyncio\nimport datetime\n",
         "import datetime"),
        ('            acc["_wx_name"] = self.captured.get("wx_name") or acc.get("_wx_name", "")\n            self._save_config()',
         '            acc["_wx_name"] = self.captured.get("wx_name") or acc.get("_wx_name", "")\n'
         '            acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")\n'
         '            self._save_config()',
         "relogin login_at"),
        ('            dup["_wx_name"] = self.captured.get("wx_name") or dup.get("_wx_name", "")\n            dup["profile_dir"] = final_dir',
         '            dup["_wx_name"] = self.captured.get("wx_name") or dup.get("_wx_name", "")\n'
         '            dup["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")\n'
         '            dup["profile_dir"] = final_dir',
         "dedup login_at"),
        ('            "profile_dir": final_dir,\n            "auto_comment_enabled": False,',
         '            "profile_dir": final_dir,\n'
         '            "login_at": datetime.datetime.now().isoformat(timespec="seconds"),\n'
         '            "auto_comment_enabled": False,',
         "new-account login_at"),
    ],
    r"\backend\account_manager.py": [
        ('            "has_aid": bool(acc.get("_aid")),\n            "wx_name": acc.get("_wx_name", ""),',
         '            "has_aid": bool(acc.get("_aid")),\n            "wx_name": acc.get("_wx_name", ""),\n'
         '            "login_at": acc.get("login_at"),',
         "status login_at"),
    ],
    r"\frontend\src\lib\api.ts": [
        ("  wx_name: string\n  locked: boolean",
         "  wx_name: string\n  login_at: string | null\n  locked: boolean",
         "AccountStatus.login_at"),
    ],
}

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
PATCHES[r"\frontend\src\pages\AccountsPage.tsx"] = [
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
]

SELFCHECK = {
    r"\backend\login_capture.py": [
        "import datetime",
        'acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")',
        'dup["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")',
        '"login_at": datetime.datetime.now().isoformat(timespec="seconds"),',
    ],
    r"\backend\account_manager.py": ['"login_at": acc.get("login_at"),'],
    r"\frontend\src\lib\api.ts": ["login_at: string | null"],
    r"\frontend\src\pages\AccountsPage.tsx": [
        "LoginCountdown", "LOGIN_TTL_MS", "登录态已过期", "计时未知",
    ],
}


def deliver(rel, content_bytes):
    """UNC 同卷暂存 + rename 覆盖 + 逐字节校验。"""
    unc_path = UNC + rel
    stage = unc_path + f".stage_{TAG}"
    with io.open(stage, "wb") as f:
        f.write(content_bytes)
    os.replace(stage, unc_path)
    with io.open(unc_path, "rb") as f:
        now = f.read()
    assert now == content_bytes, f"校验失败: {rel}"
    print(f"[DELIVER] {rel} ({len(content_bytes)} bytes, 已校验)")


def main():
    # 0) 清理历史探测残留
    for junk in (UNC + r"\backend\.probe_new", UNC + r"\backend\.probe_mod",
                 UNC + r"\_probe_dir\.keep", UNC + r"\README.md.stage_rev20",
                 UNC + r"\README.md.stage_restore"):
        try:
            os.remove(junk)
            print("[CLEAN]", junk)
        except Exception:
            pass
    try:
        os.rmdir(UNC + r"\_probe_dir")
        print("[CLEAN] _probe_dir")
    except Exception:
        pass

    # 1) 读 UNC 母本 -> 打补丁 -> 自校验 -> 写本地镜像 -> 投递 UNC
    for rel, edits in PATCHES.items():
        unc_path, loc_path = UNC + rel, LOCAL + rel
        with io.open(unc_path, encoding="utf-8", newline="") as f:
            s = f.read()
        for old, new, tag in edits:
            n = s.count(old)
            if n != 1:
                sys.exit(f"[FAIL] {rel} :: {tag} 命中 {n} 次(要求 1),已中止")
            s = s.replace(old, new, 1)
        for key in SELFCHECK[rel]:
            if key not in s:
                sys.exit(f"[FAIL] {rel} 自校验缺少: {key},已中止")
        data = s.encode("utf-8")
        # 本地镜像(供构建)
        with io.open(loc_path, "w", encoding="utf-8", newline="") as f:
            f.write(s)
        # 远程投递
        deliver(rel, data)
    print("PATCH+DELIVER ALL DONE")


if __name__ == "__main__":
    main()
