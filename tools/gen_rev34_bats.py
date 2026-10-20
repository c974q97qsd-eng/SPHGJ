# -*- coding: utf-8 -*-
"""rev34: 生成两个用户侧脚本(GBK 编码,与现有 bat 风格一致)

  1) 一键安装环境.bat   —— 从 git 克隆下来的干净目录,一键补齐运行环境
  2) 一键更新.bat       —— git pull 到最新版 + 按需重装依赖 + 启动
  3) 启动-SPHGJ.bat     —— 改为自探测 Python(便携版优先,回退系统 python),
                          这样即使没有「配置环境安装/」目录也能启动

生成后自校验:文件存在 / 是 GBK 可解码 / 关键片段齐全 / 无 CP936 无法编码的字符。
"""
import io
import os
import sys

ROOT = r"D:\SPHGJ"

# ----------------------------------------------------------------------------
# 1. 一键安装环境.bat
# ----------------------------------------------------------------------------
INSTALL = r'''@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
title SPHGJ 一键安装环境
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PORTABLE=%~dp0配置环境安装\Python314\python.exe"
set "OFFLINE_PW=%~dp0配置环境安装\ms-playwright"
set "PYEXE="
set "PYTHONIOENCODING=gbk"

echo ==========================================
echo   SPHGJ 一键安装环境
echo   适用: 从 git 克隆 / 解压得到的新目录
echo ==========================================
echo   项目目录: %ROOT%
echo   说明: 全程只需联网,不需要预先装任何东西
echo.

REM ==================== 1. 定位 Python ====================
echo [1/6] 定位 Python ...
if exist "%PORTABLE%" (
    set "PYEXE=%PORTABLE%"
    echo   [OK] 使用内置便携版 Python (免安装,已带全部依赖)
    goto :py_ok
)
for /f "delims=" %%i in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%i"
if defined PYEXE (
    echo   [OK] 使用系统 Python: !PYEXE!
    goto :py_ok
)

echo   [提示] 没找到 Python,尝试用 winget 自动装 Python 3.14 ...
where winget >nul 2>&1
if errorlevel 1 (
    echo   [错误] 本机没有 winget,无法自动安装
    echo          请手动装 Python 3.11 或更高: https://www.python.org/downloads/
    echo          安装时务必勾选 Add python.exe to PATH,装完重跑本脚本
    goto :fail
)
winget install --id Python.Python.3.14 -e --silent --accept-package-agreements --accept-source-agreements
for /f "delims=" %%i in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE (
    echo   [提示] 装完了但当前窗口的环境变量还没刷新
    echo          请关闭本窗口,重新双击 一键安装环境.bat
    goto :fail
)

:py_ok
"%PYEXE%" -c "import sys;print('  [OK] Python',sys.version.split()[0]);sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 (
    echo   [错误] Python 版本过低,需要 3.10 或更高
    goto :fail
)
echo.

REM ==================== 2. Python 依赖 ====================
echo [2/6] 安装 Python 依赖 (requirements-lock.txt) ...
"%PYEXE%" -m pip install --upgrade pip --disable-pip-version-check -q
"%PYEXE%" -m pip install -r "%ROOT%requirements-lock.txt" --disable-pip-version-check
if errorlevel 1 (
    echo   默认源失败,改用清华镜像重试 ...
    "%PYEXE%" -m pip install -r "%ROOT%requirements-lock.txt" --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo   [错误] 依赖安装失败,请检查网络或代理
        goto :fail
    )
)
echo   [OK] 依赖安装完成
echo.

REM ==================== 3. 校验依赖可导入 ====================
echo [3/6] 校验关键依赖 ...
"%PYEXE%" -c "import playwright,fastapi,uvicorn,websockets,webview,psutil;print('  [OK] 6 个关键依赖全部可导入')"
if errorlevel 1 (
    echo   [错误] 依赖导入失败,请把上面的报错发给技术
    goto :fail
)
echo.

REM ==================== 4. Chromium 浏览器 ====================
echo [4/6] 准备 Chromium 浏览器 (Playwright) ...
set "PWREADY="
if exist "%OFFLINE_PW%" (
    for /d %%D in ("%OFFLINE_PW%\chromium-*") do set "PWREADY=1"
    if defined PWREADY (
        set "PLAYWRIGHT_BROWSERS_PATH=%OFFLINE_PW%"
        echo   [OK] 使用内置离线浏览器,免下载
    )
)
if not defined PWREADY (
    for /d %%D in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do set "PWREADY=1"
    if defined PWREADY echo   [OK] 本机已有 Chromium,跳过下载
)
if not defined PWREADY (
    echo   正在下载 Chromium,约 150MB,请耐心等 ...
    "%PYEXE%" -m playwright install chromium
    if errorlevel 1 (
        echo   官方源失败,改用 npmmirror 镜像重试 ...
        set "PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright"
        "%PYEXE%" -m playwright install chromium
    )
    for /d %%D in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do set "PWREADY=1"
    if not defined PWREADY (
        echo   [错误] Chromium 下载失败
        echo          可手动执行: "%PYEXE%" -m playwright install chromium
        goto :fail
    )
    echo   [OK] Chromium 就绪
)
echo.

REM ==================== 5. WebView2 / .NET ====================
echo [5/6] 检查桌面窗口运行库 ...
REM 双判据: 文件系统 + 注册表。只查注册表的话,遇到 reg.exe 被策略管控
REM 或键被清理但程序还在的机器会误判成「缺少」,白跑一遍安装。
set "WV2OK="
for /d %%D in ("%ProgramFiles(x86)%\Microsoft\EdgeWebView\Application\*") do if exist "%%D\msedgewebview2.exe" set "WV2OK=1"
if defined WV2OK goto :wv2_done
set "WV2REG=HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
reg query "%WV2REG%" /v pv >nul 2>&1
if not errorlevel 1 set "WV2OK=1"
reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>&1
if not errorlevel 1 set "WV2OK=1"
if defined WV2OK goto :wv2_done

echo   [缺少] WebView2 Runtime (桌面窗口必需),正在自动安装 ...
if exist "%ROOT%配置环境安装\packages\MicrosoftEdgeWebview2Setup.exe" (
    "%ROOT%配置环境安装\packages\MicrosoftEdgeWebview2Setup.exe" /silent /install
) else (
    where winget >nul 2>&1
    if errorlevel 1 (
        echo   [警告] 无法自动安装,请手动下载安装:
        echo          https://developer.microsoft.com/microsoft-edge/webview2/
    ) else (
        winget install --id Microsoft.EdgeWebView2Runtime -e --silent --accept-package-agreements --accept-source-agreements
    )
)
for /d %%D in ("%ProgramFiles(x86)%\Microsoft\EdgeWebView\Application\*") do if exist "%%D\msedgewebview2.exe" set "WV2OK=1"
reg query "%WV2REG%" /v pv >nul 2>&1
if not errorlevel 1 set "WV2OK=1"
:wv2_done
if defined WV2OK (
    echo   [OK] WebView2 Runtime 已就绪
) else (
    echo   [警告] WebView2 仍未就绪,桌面窗口可能打不开
    echo          手动下载: https://developer.microsoft.com/microsoft-edge/webview2/
)
:net_check
dotnet --list-runtimes >nul 2>&1
if errorlevel 1 (
    echo   [警告] 未检测到 .NET 运行时,桌面窗口可能打不开
    echo          请安装 .NET 8 Desktop Runtime:
    echo          https://dotnet.microsoft.com/download/dotnet/8.0
) else (
    echo   [OK] .NET 运行时已安装
)
echo.

REM ==================== 6. 前端界面 ====================
echo [6/6] 检查前端界面文件 ...
if exist "%ROOT%frontend\dist\index.html" (
    echo   [OK] 前端界面已就绪 (frontend\dist 随仓库提供)
    goto :front_done
)
where npm >nul 2>&1
if errorlevel 1 (
    echo   [警告] 缺少 frontend\dist,且本机没有 Node.js
    echo          请装 Node.js 18+: https://nodejs.org/
    echo          然后在 frontend 目录执行 npm install 和 npm run build
    goto :front_done
)
echo   frontend\dist 缺失,改用源码构建(首次较慢) ...
pushd "%ROOT%frontend"
if not exist "node_modules" call npm install
call npm run build
popd
if exist "%ROOT%frontend\dist\index.html" (
    echo   [OK] 前端构建完成
) else (
    echo   [警告] 前端构建失败,界面可能打不开
)
:front_done
echo.

echo ==========================================
echo   安装完成
echo   以后双击根目录的 启动-SPHGJ.bat 运行
echo   想升级到最新版,双击 一键更新.bat
echo ==========================================
echo.
set "GO="
set /p "GO=是否现在启动 SPHGJ ? 直接回车=启动, 输入 N=不启动: "
if /i "%GO%"=="N" goto :end
call "%ROOT%启动-SPHGJ.bat"
goto :end

:fail
echo.
echo ==========================================
echo   安装失败,请按上面的提示处理
echo   常见原因:
echo     1. 网络不通 - 换手机热点 / 挂代理后重跑
echo     2. 没有 Python - 装 3.11+ 并勾选 Add to PATH
echo     3. 公司电脑限制 - 改用完整离线包(内含便携 Python)
echo ==========================================

:end
echo.
pause
'''


# ----------------------------------------------------------------------------
# 2. 一键更新.bat
# ----------------------------------------------------------------------------
UPDATE = r'''@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
title SPHGJ 一键更新
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PORTABLE=%~dp0配置环境安装\Python314\python.exe"
set "PYEXE="
set "PYTHONIOENCODING=gbk"

echo ==========================================
echo   SPHGJ 一键更新到 git 最新版
echo ==========================================
echo   项目目录: %ROOT%
REM git 默认用 UTF-8 输出提交信息,而控制台是 936 -> 中文提交信息会乱码,
REM 所以下面每条带输出的 git 命令都加了 -c i18n.logOutputEncoding=gbk
echo   本地账号 / 配置 / 数据(data、config.json、profiles)不会被覆盖
echo.

REM ==================== 1. 环境与状态 ====================
echo [1/5] 检查更新环境 ...
where git >nul 2>&1
if errorlevel 1 (
    echo   [错误] 没找到 git
    echo          请先安装 Git: https://git-scm.com/download/win
    echo          安装时选 Git from the command line,装完重开本窗口
    goto :fail
)
if not exist "%ROOT%.git" (
    echo   [错误] 当前目录不是 git 仓库,无法在线更新
    echo          这说明你拿到的是压缩包版本,请改用完整包覆盖安装
    goto :fail
)
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "OLD=%%i"
echo   [OK] 当前版本: !OLD!

set "RUNNING="
netstat -ano | findstr ":8712" | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
    set "RUNNING=1"
    echo   [提示] 检测到程序正在运行,更新完请到界面里点「重启」按钮生效
)
echo.

REM ==================== 2. 拉取最新版 ====================
echo [2/5] 拉取最新代码 ...
git fetch origin --prune
if errorlevel 1 (
    echo   [错误] 连不上远程仓库,请检查网络 (github 需能访问)
    goto :fail
)
git pull --ff-only
if errorlevel 1 (
    echo.
    echo   [错误] 更新被拒绝: 本地有文件被改过,和最新版冲突
    echo          受影响文件如下:
    git -c i18n.logOutputEncoding=gbk -c core.quotepath=false status --short
    echo.
    echo          处理办法(二选一):
    echo            A. 想保留本地改动: 先手工合并,再执行 git pull
    echo            B. 放弃本地改动: 执行 git stash 后重新双击本脚本
    goto :fail
)
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "NEW=%%i"
echo.

if "!OLD!"=="!NEW!" (
    echo   [OK] 已是最新版本 !NEW!,没有可更新的内容
    goto :after_update
)
echo   本次更新 !OLD! 到 !NEW! :
git -c i18n.logOutputEncoding=gbk -c core.quotepath=false --no-pager log --oneline "!OLD!..!NEW!"
echo.
echo   改动文件:
git -c i18n.logOutputEncoding=gbk -c core.quotepath=false --no-pager diff --name-only "!OLD!" "!NEW!"
echo.

REM ==================== 3. 依赖按需重装 ====================
echo [3/5] 检查 Python 依赖 ...
git diff --name-only "!OLD!" "!NEW!" | findstr /i "requirements" >nul
if errorlevel 1 (
    echo   [OK] 依赖清单没变,跳过 pip 安装
    goto :after_deps
)

if exist "%PORTABLE%" (
    set "PYEXE=%PORTABLE%"
) else (
    for /f "delims=" %%i in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PYEXE set "PYEXE=%%i"
)
if not defined PYEXE (
    echo   [错误] 依赖有变化,但本机找不到 Python 无法安装
    echo          请先跑一遍 一键安装环境.bat
    goto :fail
)
echo   依赖清单有变化,正在重新安装 (Python: !PYEXE!) ...
"%PYEXE%" -m pip install -r "%ROOT%requirements-lock.txt" --disable-pip-version-check
if errorlevel 1 (
    echo   默认源失败,改用清华镜像重试 ...
    "%PYEXE%" -m pip install -r "%ROOT%requirements-lock.txt" --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo   [错误] 依赖安装失败,请检查网络
        goto :fail
    )
)
echo   [OK] 依赖已更新
:after_deps
echo.

REM ==================== 4. 前端界面 ====================
echo [4/5] 检查前端界面 ...
if exist "%ROOT%frontend\dist\index.html" (
    echo   [OK] 前端界面已随更新到位
    goto :after_front
)
echo   [警告] 更新后缺少 frontend\dist,尝试用源码构建 ...
where npm >nul 2>&1
if errorlevel 1 (
    echo   [警告] 本机没有 Node.js,无法构建,界面可能打不开
    echo          请装 Node.js 18+ 后在 frontend 目录执行 npm install 和 npm run build
    goto :after_front
)
pushd "%ROOT%frontend"
if not exist "node_modules" call npm install
call npm run build
popd
if exist "%ROOT%frontend\dist\index.html" (
    echo   [OK] 前端构建完成
) else (
    echo   [警告] 前端构建失败,界面可能打不开
)
:after_front
echo.

:after_update
REM ==================== 5. 收尾 ====================
echo [5/5] 完成 ...
if defined RUNNING (
    echo   [提示] 程序正在运行: 请到界面里点「重启」,或手动关掉后重新双击 启动-SPHGJ.bat
    echo          前端若有改动,浏览器里按 Ctrl+F5 强制刷新一次
    goto :end
)
set "GO="
set /p "GO=是否现在启动 SPHGJ ? 直接回车=启动, 输入 N=不启动: "
if /i "%GO%"=="N" goto :end
call "%ROOT%启动-SPHGJ.bat"
goto :end

:fail
echo.
echo ==========================================
echo   更新失败,请按上面的提示处理
echo ==========================================

:end
echo.
pause
'''


# ----------------------------------------------------------------------------
# 3. 启动-SPHGJ.bat(自探测,兼容「有便携版」和「干净克隆」两种目录)
# ----------------------------------------------------------------------------
LAUNCH = r'''@echo off
chcp 936 >nul
cd /d "%~dp0"

set "PORTABLE=%~dp0配置环境安装\Python314\python.exe"
if exist "%~dp0配置环境安装\ms-playwright" set "PLAYWRIGHT_BROWSERS_PATH=%~dp0配置环境安装\ms-playwright"

if exist "%PORTABLE%" (
    "%PORTABLE%" main.py
    goto :done
)

where python.exe >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 本机没有找到 Python,程序无法启动
    echo        请先双击根目录的 一键安装环境.bat
    echo.
    pause
    exit /b 1
)
python main.py

:done
echo.
echo ============================================
echo 程序已退出,按任意键关闭窗口 ...
pause >nul
'''


def tame_echo(text):
    """把 echo 行里的半角括号换成全角。

    cmd.exe 在 `if ... ( ... )` 块里遇到未转义的 `)` 会提前结束块(报
    「此时不应有 xxx」),`(` 也会开启新块。echo 文本里的括号是最容易漏的坑,
    统一换成全角括号,既不影响阅读也不用到处 ^ 转义。
    """
    out = []
    for line in text.split("\n"):
        st = line.strip().lower()
        if st.startswith("echo") and not st.startswith("echo."):
            body = line
            body = body.replace("(", "（").replace(")", "）")
            out.append(body)
        else:
            out.append(line)
    return "\n".join(out)


def write_gbk(name, text):
    path = os.path.join(ROOT, name)
    data = text.replace("\n", "\r\n").encode("gbk")  # bat 需要 CRLF,控制台是 936
    with open(path, "wb") as f:
        f.write(data)
    return path, len(data)


def main():
    targets = [
        ("一键安装环境.bat", tame_echo(INSTALL)),
        ("一键更新.bat", tame_echo(UPDATE)),
        ("启动-SPHGJ.bat", tame_echo(LAUNCH)),
    ]
    for name, text in targets:
        path, size = write_gbk(name, text)
        print(f"写入 {path}  ({size} 字节)")

    # ---------------- 自校验 ----------------
    print("\n===== SELFCHECK =====")
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            ok = False

    for name, _ in targets:
        path = os.path.join(ROOT, name)
        with open(path, "rb") as f:
            raw = f.read()
        try:
            txt = raw.decode("gbk")
            dec = True
        except Exception:
            txt, dec = "", False
        chk(os.path.isfile(path) and len(raw) > 300, f"{name} 存在且非空 ({len(raw)} 字节)")
        chk(dec, f"{name} 可按 GBK 解码")
        chk(b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{name} 全 CRLF 无裸 LF")
        chk(txt.startswith("@echo off\r\n"), f"{name} 首行是 @echo off")
        chk("chcp 936" in txt, f"{name} 设置了 chcp 936")

    with open(os.path.join(ROOT, "一键安装环境.bat"), "rb") as f:
        a = f.read().decode("gbk")
    for key in ["requirements-lock.txt", "playwright install chromium",
                "pypi.tuna.tsinghua.edu.cn", "WebView2", "frontend\\dist",
                "Add python.exe to PATH"]:
        chk(key in a, f"一键安装环境.bat 含关键片段: {key}")

    with open(os.path.join(ROOT, "一键更新.bat"), "rb") as f:
        b = f.read().decode("gbk")
    for key in ["git fetch origin --prune", "git pull --ff-only", "status --short",
                "requirements", "frontend\\dist", "netstat -ano", ":8712",
                "不会被覆盖"]:
        chk(key in b, f"一键更新.bat 含关键片段: {key}")

    with open(os.path.join(ROOT, "启动-SPHGJ.bat"), "rb") as f:
        c = f.read().decode("gbk")
    for key in ["配置环境安装\\Python314\\python.exe", "where python.exe",
                "一键安装环境.bat", "main.py"]:
        chk(key in c, f"启动-SPHGJ.bat 含关键片段: {key}")

    for name, _ in targets:
        with open(os.path.join(ROOT, name), "rb") as f:
            txt = f.read().decode("gbk")
        bad = [ln for ln in txt.splitlines()
               if ln.strip().lower().startswith("echo") and ("(" in ln or ")" in ln)]
        chk(not bad, f"{name} 的 echo 行无半角括号" + ("" if not bad else f" -> {bad[:2]}"))

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
