@echo off
chcp 936 >nul
setlocal EnableDelayedExpansion
title SPHGJ 一键更新
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PORTABLE=%~dp0配置环境安装\Python314\python.exe"
set "PYEXE="
set "PYTHONIOENCODING=gbk"

REM 运行主机(母本)标记:.no_git_update 存在就不碰 git 直接退出。
REM 母本的代码由开发机投递同步,它的 .git 是个没 remote 的空壳;
REM 在这台机器上跑更新会去装 Git、改 .git/config,只会制造混乱。
if exist "%~dp0.no_git_update" (
    echo ==========================================
    echo   本机是「运行主机」,不走 git 更新
    echo ==========================================
    echo   这台机器的代码由开发机投递同步,不需要也不应该跑一键更新。
    echo   要更新到最新代码,请到开发机上投递,然后在界面上点重启。
    echo.
    echo   如果这是一台普通用户机器、只是误留了这个标记文件,
    echo   删掉项目目录下的 .no_git_update 再双击本脚本即可。
    echo.
    pause
    exit /b 0
)

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
REM git 先「自动找」再「自动装」:PATH 里没有不代表没装(装的时候没勾
REM Add to PATH、或装在 D 盘 Program Files 下,都很常见),所以不能只 where。
set "GIT="
call :find_git
if not defined GIT (
    echo   [提示] 本机没装 Git,自动安装 - 约 60MB,中途无需操作,请耐心等 ...
    call :install_git
)
if not defined GIT (
    echo   [错误] Git 自动安装没成功
    echo          请手动装一次: https://git-scm.com/download/win
    echo          一路默认下一步即可,装完重新双击 一键更新.bat
    echo          每台机器只需要装一次,之后所有更新都不再需要
    goto :fail
)
REM 刚装好的 git 不会进当前进程的环境变量,这里补一次 PATH,
REM 后面所有命令就能照常写 git,不用重开窗口
for %%i in ("%GIT%") do set "PATH=%%~dpi;%PATH%"
for /f "delims=" %%i in ('git --version 2^>nul') do set "GITVER=%%i"
echo   [OK] !GITVER!

if not exist "%ROOT%.git" (
    echo   [错误] 当前目录不是 git 仓库,无法在线更新
    echo          这说明你拿到的是压缩包版本,不是 git clone 下来的
    echo          请改用完整包覆盖安装 - 只覆盖代码,别覆盖 data / config.json
    goto :fail
)

REM 有些目录是从别处拷来的 .git,里面没有 remote -> 拉不到东西。
REM 没有就自动补一个(只读拉取用 https 就够,不需要密钥)。
set "ORIGIN="
for /f "delims=" %%i in ('git remote get-url origin 2^>nul') do set "ORIGIN=%%i"
if not defined ORIGIN (
    echo   [提示] 这个目录没有配置远程地址,自动补一个 ...
    git remote add origin https://github.com/c974q97qsd-eng/SPHGJ.git
    if errorlevel 1 (
        echo   [错误] 自动配置远程地址失败,请手动执行:
        echo          git remote add origin https://github.com/c974q97qsd-eng/SPHGJ.git
        goto :fail
    )
    set "ORIGIN=https://github.com/c974q97qsd-eng/SPHGJ.git"
    echo   [OK] 已指向 https://github.com/c974q97qsd-eng/SPHGJ
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

REM 当前分支(取不到或处于分离头指针就用 master)
set "BRANCH="
for /f "delims=" %%i in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%i"
if not defined BRANCH set "BRANCH=master"
if /i "!BRANCH!"=="HEAD" set "BRANCH=master"

REM 直连 GitHub。30 秒内低于 1000 B/s 就主动放弃,免得卡着不动
set "GOTFETCH="
set "VIA=GitHub 直连"
echo   正在连接 github.com ...
git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch origin "+refs/heads/!BRANCH!:refs/remotes/origin/!BRANCH!" >nul 2>&1
if not errorlevel 1 set "GOTFETCH=1"
if defined GOTFETCH goto :fetch_done

echo   [提示] 直连失败 - 常见原因是连接被重置,国内网络访问 github 经常这样
echo          自动换国内镜像通道重试,请稍候,单个通道最多等 30 秒 ...
echo.

set "VIA=ghfast.top 镜像"
call :try_mirror "https://ghfast.top/https://github.com/c974q97qsd-eng/SPHGJ.git" "!BRANCH!"
if not errorlevel 1 set "GOTFETCH=1"
if defined GOTFETCH goto :fetch_done

set "VIA=gh-proxy.com 镜像"
call :try_mirror "https://gh-proxy.com/https://github.com/c974q97qsd-eng/SPHGJ.git" "!BRANCH!"
if not errorlevel 1 set "GOTFETCH=1"
if defined GOTFETCH goto :fetch_done

echo.
echo   [错误] 所有通道都拉不到远程仓库,对号入座看是哪一种:
echo.
echo          当前远程地址: !ORIGIN!
echo          1. 这台机器完全上不了外网: 换手机热点再重跑
echo          2. 公司网络只放行代理: 先设好代理再重跑本脚本
echo             set HTTPS_PROXY=http://代理地址:端口
echo          3. 用的是 SSH 地址但本机没密钥: 换成 https
echo             git remote set-url origin https://github.com/c974q97qsd-eng/SPHGJ.git
echo          4. 以上都不行: 从云盘下载最新 zip 包,解压覆盖到本目录
echo             只覆盖程序文件,data\ config.json profiles\ 都会保留
goto :fail

:fetch_done
echo   [OK] 已取到最新代码,通道: !VIA!
echo.

REM rev51: 不再直接引用 FETCH_HEAD —— 它只是个临时文件,fetch 被打断
REM 或历史上从没成功 fetch 过时就根本不存在,git 会报
REM   fatal: ambiguous argument 'FETCH_HEAD'
REM 现在的做法: fetch 时已显式写到 refs\remotes\origin\分支,优先用它。
set "TARGET=refs/remotes/origin/!BRANCH!"
git rev-parse --verify "!TARGET!" >nul 2>&1
if not errorlevel 1 goto :have_target
REM 老版本 git 或不支持该 refspec 时的兜底:退回临时文件
git rev-parse --verify FETCH_HEAD >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 已经连上了远程,但本地找不到要更新的版本引用
    echo          这说明拉取过程被中途打断了。请重跑一次本脚本。
    echo          如果反复出现: 从云盘下载最新 zip 包解压覆盖到本目录
    echo.
    goto :fail
)
set "TARGET=FETCH_HEAD"

:have_target
git merge --ff-only "!TARGET!"
if errorlevel 1 (
    echo.
    echo   [错误] 没法直接快进到最新版,三种情况对号入座:
    echo.
    echo          A. 本机程序文件被改过,和最新版冲突。受影响文件:
    git -c i18n.logOutputEncoding=gbk status --short
    echo            处理: 想保留改动就先手工合并;不想要就 git stash 后重跑
    echo.
    echo          B. 提示 frontend\dist 里文件将被覆盖 - 旧版没把前端产物入库
    echo            处理: 删掉 frontend\dist 整个目录后重跑,git 会自动还原
    echo.
    echo          C. 本机版本和远端不是同一条历史 - 很早以前克隆的老仓库
    echo            处理: 下面可以直接对齐到最新版。只替换程序文件,
    echo                  data\ config.json profiles\ 一律不动,账号不会掉
    echo.
    set "ANS="
    set /p "ANS=  要现在对齐到最新版吗? 输入 Y 回车确认,直接回车放弃: "
    if /i not "!ANS!"=="Y" goto :fail
    echo   正在对齐到最新版 ...
    REM 输出静默: reset 会把 commit message 原样打到 UTF-8,936 控制台下是乱码
    git reset --hard "!TARGET!" >nul
    if errorlevel 1 (
        echo   [错误] 对齐失败,请把上面的报错截图给维护者
        goto :fail
    )
    echo   [OK] 已对齐到最新版
)
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "NEW=%%i"
echo.

if "!OLD!"=="!NEW!" (
    echo   [OK] 已是最新版本 !NEW!,没有可更新的内容
    goto :after_update
)
echo   本次更新 !OLD! 到 !NEW! :
git -c i18n.logOutputEncoding=gbk --no-pager log --oneline "!OLD!..!NEW!"
echo.
echo   改动文件:
git -c i18n.logOutputEncoding=gbk --no-pager diff --name-only "!OLD!" "!NEW!"
echo   上面带 \346\201 这类编码的是中文文件名,在 936 控制台下正常显示就是这样
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

:try_mirror
REM %1 = 镜像完整地址, %2 = 分支名
echo   - 尝试 %~1
git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch "%~1" "+refs/heads/%~2:refs/remotes/origin/%~2" --no-tags >nul 2>&1
if errorlevel 1 (
    echo     [失败] 这个通道不通
    exit /b 1
)
echo     [成功]
exit /b 0

:fail
echo.
echo ==========================================
echo   更新失败,请按上面的提示处理
echo ==========================================

:end
echo.
pause
goto :eof


REM ============================================================
REM 子过程: 找 git —— 不要求它在 PATH 里
REM   返回值: 变量 GIT = git.exe 的完整路径(找不到则保持未定义)
REM ============================================================
:find_git
REM 1) PATH 里直接有
for /f "delims=" %%i in ('where git.exe 2^>nul') do if not defined GIT set "GIT=%%i"
if defined GIT goto :eof
REM 2) 常见安装目录 —— 必须遍历各盘符,有机器把 Git 装在 D 盘 Program Files
for %%D in (C D E F G) do (
    if not defined GIT if exist "%%D:\Program Files\Git\cmd\git.exe" set "GIT=%%D:\Program Files\Git\cmd\git.exe"
    if not defined GIT if exist "%%D:\Program Files (x86)\Git\cmd\git.exe" set "GIT=%%D:\Program Files (x86)\Git\cmd\git.exe"
    if not defined GIT if exist "%%D:\Git\cmd\git.exe" set "GIT=%%D:\Git\cmd\git.exe"
)
if defined GIT goto :eof
REM 3) 装到用户目录的情况(免管理员安装的选项)
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
goto :eof


REM ============================================================
REM 子过程: 装 git —— winget 优先,不行就下官方安装包静默装
REM ============================================================
:install_git
where winget >nul 2>&1
if errorlevel 1 goto :install_git_pkg
echo   -- 用 winget 安装 Git --
winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
call :find_git
if defined GIT goto :eof
echo   winget 这条路没走通,改用官方安装包 ...

:install_git_pkg
echo   -- 下载官方安装包并静默安装（约 60MB）--
set "GSETUP=%TEMP%\sphgj-git-setup.exe"
if exist "%GSETUP%" del /f /q "%GSETUP%" >nul 2>&1
REM 走 GitHub API 取最新 64 位安装包的下载地址,不写死版本号
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';$r=Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers @{'User-Agent'='sphgj'};$a=$r.assets|Where-Object{$_.name -like 'Git-*-64-bit.exe'}|Select-Object -First 1;if(-not $a){exit 2};Invoke-WebRequest $a.browser_download_url -OutFile '%GSETUP%'"
if not exist "%GSETUP%" (
    echo   安装包下载失败
    goto :eof
)
echo   正在静默安装（1~2 分钟）...
"%GSETUP%" /VERYSILENT /NORESTART /NOCANCEL /SP- /o:PathOption=Cmd
call :find_git
if defined GIT echo   [OK] Git 安装完成
goto :eof
