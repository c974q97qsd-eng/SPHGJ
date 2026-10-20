@echo off
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
echo   本地账号 / 配置 / 数据（data、config.json、profiles）不会被覆盖
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
    echo   [错误] 连不上远程仓库,请检查网络 （github 需能访问）
    goto :fail
)
git pull --ff-only
if errorlevel 1 (
    echo.
    echo   [错误] 更新被拒绝: 本地有文件被改过,和最新版冲突
    echo          受影响文件如下:
    git -c i18n.logOutputEncoding=gbk -c core.quotepath=false status --short
    echo.
    echo          处理办法（二选一）:
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
echo   依赖清单有变化,正在重新安装 （Python: !PYEXE!） ...
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
