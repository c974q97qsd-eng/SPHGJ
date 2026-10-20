@echo off
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
    echo   [OK] 使用内置便携版 Python （免安装,已带全部依赖）
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
echo [2/6] 安装 Python 依赖 （requirements-lock.txt） ...
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
echo [4/6] 准备 Chromium 浏览器 （Playwright） ...
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

echo   [缺少] WebView2 Runtime （桌面窗口必需）,正在自动安装 ...
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
    echo   [OK] 前端界面已就绪 （frontend\dist 随仓库提供）
    goto :front_done
)
where npm >nul 2>&1
if errorlevel 1 (
    echo   [警告] 缺少 frontend\dist,且本机没有 Node.js
    echo          请装 Node.js 18+: https://nodejs.org/
    echo          然后在 frontend 目录执行 npm install 和 npm run build
    goto :front_done
)
echo   frontend\dist 缺失,改用源码构建（首次较慢） ...
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
echo     3. 公司电脑限制 - 改用完整离线包（内含便携 Python）
echo ==========================================

:end
echo.
pause
