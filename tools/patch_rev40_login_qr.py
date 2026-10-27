# -*- coding: utf-8 -*-
"""rev40: 重登默认不弹浏览器窗口,统一走网页二维码 UI

用户在账号管理里定的规矩:
  - 即使是本机(控制台就跑在这台机器上),重新登录时默认也不弹 Chromium 窗口
  - 默认走「二维码推到前端网页」这套登录 UI
  - 想弹窗仍可:前端点「本机窗口」,或 config.json 里 "login_popup_window": true

改点:
  1) backend/server.py        新增 _login_popup_window(),两个登录接口的默认值改用它
  2) backend/account_manager.py  start_relogin 默认 headed=False(自动重登队列也走二维码)
  3) frontend 文案注释          本机默认也是网页二维码
  4) README                    写清默认行为与两种恢复弹窗的方式
"""
import io

PATCHES = [
    # ================= backend/server.py =================
    (
        r"D:\SPHGJ\backend\server.py",
        '''    host = (request.client.host if request.client else "") or ""
    host = host.replace("::ffff:", "")  # IPv4-mapped IPv6 归一化
    return host in _local_ips()


@app.post("/api/accounts/login/start")''',
        '''    host = (request.client.host if request.client else "") or ""
    host = host.replace("::ffff:", "")  # IPv4-mapped IPv6 归一化
    return host in _local_ips()


def _login_popup_window() -> bool:
    """重新登录时是否弹本机浏览器窗口。

    rev40: 默认 False —— 不管本机还是远程,统一走「二维码推到前端网页」这套 UI。
    本机以前默认弹窗(能直接扫),但会抢焦点、盖住控制台,而且和远程是两套体验。
    要恢复弹窗:config.json 里加 "login_popup_window": true;
    单次想让本机弹窗:前端登录弹窗点「本机窗口」。
    """
    try:
        return bool(config.get("login_popup_window", False))
    except Exception:
        return False


@app.post("/api/accounts/login/start")''',
        "server-helper",
    ),
    (
        r"D:\SPHGJ\backend\server.py",
        '''    # 未显式指定 -> 按客户端是否本机自动决定模式
    if headed is None:
        headed = _client_is_local(request)
    sess = await manager.start_login(headed=headed)''',
        '''    # 未显式指定 -> rev40 统一走网页二维码(默认不弹本机窗口)
    if headed is None:
        headed = _login_popup_window()
    sess = await manager.start_login(headed=headed)''',
        "server-login-start",
    ),
    (
        r"D:\SPHGJ\backend\server.py",
        '''    headed 未指定时按客户端是否本机自动决定(远程走网页二维码)。
    """
    if headed is None:
        headed = _client_is_local(request)
    sess = await manager.start_relogin(account_id, headed=headed)''',
        '''    headed 未指定时走网页二维码(rev40 默认不弹本机窗口)。
    """
    if headed is None:
        headed = _login_popup_window()
    sess = await manager.start_relogin(account_id, headed=headed)''',
        "server-relogin",
    ),
    # ================= backend/account_manager.py =================
    (
        r"D:\SPHGJ\backend\account_manager.py",
        '''    async def start_relogin(self, account_id, auto=False, on_finished=None, headed=True):
        """已存在账号重新扫码登录:复用原 profile_dir(headed),成功后更新原账号字段。''',
        '''    async def start_relogin(self, account_id, auto=False, on_finished=None, headed=False):
        """已存在账号重新扫码登录:复用原 profile_dir,成功后更新原账号字段。

        rev40: 默认 headless + 网页二维码推送(本机也不再弹窗口);
        只有显式 headed=True(前端点「本机窗口」或配置 login_popup_window)才弹浏览器。''',
        "am-relogin-default",
    ),
    (
        r"D:\SPHGJ\backend\account_manager.py",
        '''        # 失效账号:后端自动依次弹 headed 扫码窗口,无需用户手动点启动。
        # rev17: 软重启时传 auto_relogin=False —— 自动重启不应擅自弹扫码窗口打扰用户,''',
        '''        # 失效账号:后端自动依次推网页二维码,无需用户手动点启动。
        # rev17: 软重启时传 auto_relogin=False —— 自动重启不应擅自开始扫码打扰用户,''',
        "am-start-comment",
    ),
    (
        r"D:\SPHGJ\backend\account_manager.py",
        '''        """失效账号入队自动 relogin(headed 弹窗依次扫码);无活跃项时立即弹下一个。"""''',
        '''        """失效账号入队自动 relogin(网页二维码依次扫);无活跃项时立即开始下一个。"""''',
        "am-queue-doc",
    ),
    # ================= frontend =================
    (
        r"D:\SPHGJ\frontend\src\components\accounts\AddAccountDialog.tsx",
        '''      // 未指定模式 -> 不传 headed,由后端按客户端是否本机自动决定:
      // 本机走浏览器窗口弹窗,远程(局域网其他主机)走 headless + 网页二维码''',
        '''      // 未指定模式 -> 不传 headed,由后端决定:rev40 起默认都是
      // headless + 网页二维码(本机也不弹窗口),要弹窗需显式点「本机窗口」''',
        "fe-comment",
    ),
    # ================= README =================
    (
        r"D:\SPHGJ\README.md",
        '''4. 二维码不显示?点「在新窗口扫码」-> 弹出带界面 Chromium 扫码。''',
        '''4. 默认是「网页二维码」:二维码直接推到这个界面里扫,**本机也不弹浏览器窗口**
   (账号管理里定的规矩,保持本机和远程同一套体验)。
   想让本机弹 Chromium 窗口扫,两个办法:
   - 登录弹窗里点「本机窗口」(只对这一次生效)
   - `config.json` 里加 `"login_popup_window": true`(之后一直弹)''',
        "readme",
    ),
]

SELFCHECK = [
    (r"D:\SPHGJ\backend\server.py", ["_login_popup_window", "headed = _login_popup_window()"],
     ["headed = _client_is_local(request)"]),
    (r"D:\SPHGJ\backend\account_manager.py", ["on_finished=None, headed=False)"], ["headed=True):"]),
    (r"D:\SPHGJ\frontend\src\components\accounts\AddAccountDialog.tsx", ["本机也不弹窗口"], []),
    (r"D:\SPHGJ\README.md", ["login_popup_window", "本机也不弹浏览器窗口"], []),
]

ok = True
for path, old, new, tag in PATCHES:
    s = io.open(path, encoding="utf-8").read()
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次,应为 1 次 -> {path}")
        ok = False
        continue
    io.open(path, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
    print(f"[ok] {tag}")

print("\n--- 自校验 ---")
for path, must, mustnot in SELFCHECK:
    s = io.open(path, encoding="utf-8").read()
    for k in must:
        good = s.count(k) >= 1
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 含 {k}")
        ok &= good
    for k in mustnot:
        good = s.count(k) == 0
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 已无 {k}")
        ok &= good

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
