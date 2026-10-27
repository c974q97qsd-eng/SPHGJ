@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SYSDIR=%SystemRoot%\System32"
set "PS=%SYSDIR%\WindowsPowerShell\v1.0\powershell.exe"

echo ============================================
echo   SPHGJ 恢复启动
echo   用于程序异常退出 / 重启失败后的一键自救
echo ============================================
echo.

echo [1/3] 检查内存并清理上次没有退干净的进程
if not exist "%PS%" (
    echo        找不到 PowerShell,跳过清理
    goto :launch
)

"%PS%" -NoProfile -Command "$os=Get-CimInstance Win32_OperatingSystem; '        FREE_MB=' + [int]($os.FreePhysicalMemory/1KB) + '   TOTAL_MB='  + [int]($os.TotalVisibleMemorySize/1KB); $b=Get-Process | Where-Object { $_.Path -like '*ms-playwright*' }; if($b){ $b | Stop-Process -Force; '        已结束残留浏览器: ' + $b.Count } else { '        没有残留浏览器' }; $n=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -like '*playwright*' }; if($n){ $n | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; '        已结束残留驱动: ' + $n.Count } else { '        没有残留驱动' }"
echo.

echo [2/3] 等待 3 秒让内存释放
"%SYSDIR%\ping.exe" -n 4 127.0.0.1 >nul
echo.

:launch
echo [3/3] 启动程序
set "PORTABLE=%~dp0配置环境安装\Python314\python.exe"
if exist "%~dp0配置环境安装\ms-playwright" set "PLAYWRIGHT_BROWSERS_PATH=%~dp0配置环境安装\ms-playwright"
if exist "%PORTABLE%" (
    echo        使用便携版 Python
    "%PORTABLE%" main.py
    goto :done
)
"%SYSDIR%\where.exe" python.exe >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 本机没有找到 Python
    echo        请先双击根目录的 一键安装环境.bat
    echo.
    pause
    exit /b 1
)
echo        使用系统 Python
python main.py

:done
echo.
echo ============================================
echo 程序已退出
echo   如果上面出现 MemoryError 之类的字样,说明内存仍然不够,
echo   请重启这台电脑后再启动一次
echo ============================================
pause >nul
