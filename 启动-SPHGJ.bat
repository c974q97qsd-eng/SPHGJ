@echo off
chcp 936 >nul
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0配置环境安装\ms-playwright"
"%~dp0配置环境安装\Python314\python.exe" main.py
pause
