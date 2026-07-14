@echo off
cd /d "%~dp0"

echo ============================================
echo   Build exe (bundled Chromium, slim)
echo ============================================
echo.

echo [1/6] Install Python deps (requirements.txt)...
pip install -r requirements.txt
if errorlevel 1 (
    echo [Error] pip install failed. Check network or pip config.
    pause
    exit /b 1
)

echo.
echo [2/6] Frontend...
if exist "frontend\dist\index.html" (
    echo [OK] Frontend prebuilt dist found, skip npm build.
    goto :frontend_done
)
where npm >nul 2>&1
if errorlevel 1 (
    echo [Error] npm not found and frontend dist not prebuilt. Install Node.js 18+.
    pause
    exit /b 1
)
pushd frontend
call npm install
call npm run build
popd
if not exist "frontend\dist\index.html" (
    echo [Error] Frontend build failed.
    pause
    exit /b 1
)
:frontend_done
echo [OK] Frontend ready.

echo.
echo [3/6] Check PyInstaller...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [Install] pyinstaller...
    pip install pyinstaller
)

echo.
echo [4/6] PyInstaller packing...
if exist "dist\sphgj" rmdir /S /Q "dist\sphgj"
pyinstaller --noconsole --name sphgj --clean ^
    --add-data "frontend\dist;frontend\dist" ^
    --add-data "config.json;." ^
    --hidden-import "webview.platforms.edgechromium" ^
    --collect-all webview ^
    --collect-submodules backend ^
    --collect-submodules uvicorn ^
    --collect-submodules websockets ^
    --collect-submodules fastapi ^
    --collect-submodules starlette ^
    --collect-submodules pydantic ^
    main.py
if errorlevel 1 (
    echo [Error] PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo [5/6] Copy Playwright Chromium (latest version only, slim)...
set "SRC=%USERPROFILE%\AppData\Local\ms-playwright"
set "DST=dist\sphgj\browsers"
if not exist "%SRC%" (
    echo [Info] Chromium not found, installing...
    python -m playwright install chromium
    if errorlevel 1 (
        echo [Retry] Switching to China mirror...
        set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
        python -m playwright install chromium
    )
)
if not exist "%SRC%" (
    echo [Error] Chromium install failed. Run manually: python -m playwright install chromium
    pause
    exit /b 1
)
if not exist "%DST%" mkdir "%DST%"
rem 只拷最新 chromium 完整版(排除旧版本/ffmpeg/firefox/webkit)
for /f "delims=" %%D in ('dir /ad /b /o-n "%SRC%\chromium-*" 2^>nul') do (
    echo [OK] Copy %%D
    xcopy /E /I /Y "%SRC%\%%D" "%DST%\%%D" >nul
    goto :cp_chromium_done
)
:cp_chromium_done
rem 只拷最新 chromium-headless-shell
for /f "delims=" %%D in ('dir /ad /b /o-n "%SRC%\chromium_headless_shell-*" 2^>nul') do (
    echo [OK] Copy %%D
    xcopy /E /I /Y "%SRC%\%%D" "%DST%\%%D" >nul
    goto :cp_hs_done
)
:cp_hs_done
echo [OK] Chromium copied to %DST%

echo.
echo [6/6] Prepare data dir...
if not exist "dist\sphgj\data" mkdir "dist\sphgj\data"

echo.
echo ============================================
echo   Done!
echo   exe:    dist\sphgj\sphgj.exe
echo   browser: dist\sphgj\browsers\
echo   profiles\ and data\ created on first run.
echo ============================================
pause
