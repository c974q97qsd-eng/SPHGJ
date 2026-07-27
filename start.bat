@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
python main.py
echo.
echo ============================================
echo App exited. Press any key to close...
pause >nul
endlocal
