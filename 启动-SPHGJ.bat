@echo off
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
