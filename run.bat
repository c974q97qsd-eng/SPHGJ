@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo  [1/2] Build frontend (npm run build)
echo ============================================
where npm >nul 2>&1
if errorlevel 1 (
    echo [Error] npm not found. Install Node.js 18+ first.
    pause
    exit /b 1
)
pushd frontend
call npm run build
if errorlevel 1 (
    echo [Error] frontend build failed.
    popd
    pause
    exit /b 1
)
popd
echo [OK] frontend built.
echo.

echo ============================================
echo  [2/2] Launch app (python main.py)
echo ============================================
python main.py
echo.
echo App exited. Press any key to close...
pause >nul
endlocal
