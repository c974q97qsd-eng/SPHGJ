"""锁定冲突逻辑烟测(不启动浏览器)。

覆盖:
  1) clear_login_state 的安全闸:非 profiles 目录必须拒绝且不动文件
  2) clear_login_state 正常清理 profiles 下目录(清空 + 目录保留)
  3) _lock_mismatch:finder_id 一致/不一致、身份未知不误判、未锁定账号不判
  4) _select_page_missing_locked:选择页有锁定项->不判;页面无锁定项->判
"""
import os
import sys
import asyncio
import shutil

sys.path.insert(0, r"D:\SPHGJ")
os.chdir(r"D:\SPHGJ")

from backend.login_capture import clear_login_state, LoginSession

fails = []


def ck(cond, label, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        fails.append(label)


# ---------- 1) 安全闸:非 profiles 目录 ----------
keep = "./data/_locktest_keep"
shutil.rmtree(keep, ignore_errors=True)
os.makedirs(keep, exist_ok=True)
with open(keep + "/a.txt", "w", encoding="utf-8") as f:
    f.write("must-survive")
ok, msg = clear_login_state(keep)
ck(ok is False, "拒绝清理非 profiles 目录", f"-> {msg}")
ck(os.path.exists(keep + "/a.txt"), "非 profiles 目录内容未被删")
shutil.rmtree(keep, ignore_errors=True)

ok, msg = clear_login_state("")
ck(ok is False, "空 profile_dir 拒绝", f"-> {msg}")

# ---------- 2) 正常清理 ----------
d = "./profiles/_locktest_tmp"
shutil.rmtree(d, ignore_errors=True)
os.makedirs(d + "/Default/Network", exist_ok=True)
with open(d + "/Default/Network/Cookies", "wb") as f:
    f.write(b"fake-cookie")
with open(d + "/Local State", "w", encoding="utf-8") as f:
    f.write("{}")
ok, msg = clear_login_state(d)
ck(ok is True, "清理 profiles 下登录态", f"-> {msg}")
ck(os.path.isdir(d) and os.listdir(d) == [], "目录保留且已清空", f"-> {os.listdir(d)}")
shutil.rmtree(d, ignore_errors=True)

# ---------- 3) _lock_mismatch ----------
acc = {"id": "acc1", "name": "账号A", "_log_finder_id": "FID_X",
       "locked_finder_id": "FID_X", "locked_name": "账号A"}
s = LoginSession(playwright=None, config={}, emit=None, account=acc)

s.captured["finder_id"] = "FID_Y"
s.captured["name"] = "别的号"
c, show, reason = s._lock_mismatch()
ck(c is True and show == "别的号", "finder_id 不一致 -> 判冲突", f"-> {reason}")

s.captured["finder_id"] = "FID_X"
c, _, _ = s._lock_mismatch()
ck(c is False, "finder_id 一致 -> 不判冲突")

s.captured["finder_id"] = ""
c, _, _ = s._lock_mismatch()
ck(c is False, "身份未取到 -> 不判(交给后续检测点)")

s2 = LoginSession(playwright=None, config={}, emit=None,
                  account={"id": "acc2", "name": "账号B", "_log_finder_id": "FID_Z"})
s2.captured["finder_id"] = "FID_OTHER"
c, _, _ = s2._lock_mismatch()
ck(c is False, "未锁定账号 -> 不做任何判定")

# ---------- 4) 选择页判定 ----------
s3 = LoginSession(playwright=None, config={}, emit=None, account=acc)


async def fake_body():
    return "选择视频号登录\n别的号 运营者"


s3._body_text = fake_body
ck(asyncio.run(s3._select_page_missing_locked(
    [{"name": "账号A", "_raw": "账号A 管理员"}])) is False,
   "选择页含锁定账号 -> 不判冲突")
ck(asyncio.run(s3._select_page_missing_locked(
    [{"name": "别的号", "_raw": "别的号 运营者"}])) is True,
   "选择页无锁定账号 -> 判冲突(页面正文也没有)")


async def fake_body2():
    return "选择视频号登录\n账号C 运营者"


s3._body_text = fake_body2
ck(asyncio.run(s3._select_page_missing_locked(
    [{"name": "账号C", "_raw": "账号C 运营者"}])) is True,
   "枚举与正文都无锁定项 -> 判冲突")


async def fake_body3():
    return "选择视频号登录\n账号C 运营者\n账号A 管理员"


s3._body_text = fake_body3
ck(asyncio.run(s3._select_page_missing_locked(
    [{"name": "账号C", "_raw": "账号C 运营者"}])) is False,
   "懒加载未纳入枚举但正文有锁定名 -> 不判(防误判)")

# ---------- 5) finalize 兜底校验仍生效 ----------
from backend.login_capture import check_account_lock
s4 = LoginSession(playwright=None, config={}, emit=None, account=acc)
s4.captured["finder_id"] = "FID_Y"
s4.captured["name"] = "别的号"
ck(bool(check_account_lock(acc, s4.captured)), "finalize 兜底:check_account_lock 仍会拒绝")

print("SMOKE", "PASS" if not fails else f"FAIL {fails}")
