@echo off
cd /d "%~dp0"

echo ============================================
echo   WeChat Channels Comment Manager - Setup
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [Info] Python not found. Installing via winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [Error] winget not found. Install Python 3.10+ manually:
        echo         https://www.python.org/downloads/
        pause
        exit /b 1
    )
    winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [Error] Python install failed. Install manually:
        echo         https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [Info] Python installed. CLOSE this window and re-run setup.bat to refresh PATH.
    pause
    exit /b 0
)
echo [OK] Python:
python --version

echo.
echo [Install] Upgrading pip...
python -m pip install --upgrade pip

echo [Install] Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [Error] Dependencies install failed. Check network or pip config.
    pause
    exit /b 1
)

echo.
echo [Install] Playwright Chromium...
python -m playwright install chromium
if errorlevel 1 (
    echo [Retry] Switching to China mirror...
    set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
    python -m playwright install chromium
    if errorlevel 1 (
        echo [Error] Chromium install failed. Run manually: python -m playwright install chromium
        pause
        exit /b 1
    )
)

echo.
echo [Check] WebView2 Runtime (required for desktop window)...
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>&1
if errorlevel 1 reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>&1
if errorlevel 1 (
    echo [Info] WebView2 Runtime not found, installing via winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [Warning] winget not found. Install WebView2 Runtime manually:
        echo         https://developer.microsoft.com/microsoft-edge/webview2/
        echo         Without it the desktop window cannot start.
    ) else (
        winget install Microsoft.EdgeWebView2Runtime --silent --accept-package-agreements --accept-source-agreements
        if errorlevel 1 (
            echo [Warning] WebView2 auto-install failed. Install manually:
            echo         https://developer.microsoft.com/microsoft-edge/webview2/
        ) else (
            echo [OK] WebView2 Runtime installed.
        )
    )
) else (
    echo [OK] WebView2 Runtime detected.
)

echo.
echo [Check] Frontend prebuilt dist...
if exist "frontend\dist\index.html" (
    echo [OK] Frontend prebuilt dist found, skip Node.js/npm.
) else (
    echo [Install] Frontend deps + build -- dist not prebuilt...
    where npm >nul 2>&1
    if errorlevel 1 (
        echo [Warning] npm not found and dist not prebuilt. Install Node.js 18+: https://nodejs.org/
        echo           Then in frontend/ run: npm install ^&^& npm run build
    ) else (
        pushd frontend
        call npm install
        call npm run build
        popd
        if not exist "frontend\dist\index.html" (
            echo [Error] Frontend build failed, output missing.
            pause
            exit /b 1
        )
        echo [OK] Frontend built to frontend\dist
    )
)

echo.
echo ============================================
echo   Setup done! Run start.bat to launch.
echo ============================================
pause
