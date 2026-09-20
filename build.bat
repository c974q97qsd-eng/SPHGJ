@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================
echo   Build exe (bundled Chromium, slim)
echo ============================================
echo.

echo [1/6] Install Python deps (requirements.txt)...
python -m pip install -r requirements.txt
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
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [Install] pyinstaller...
    python -m pip install pyinstaller
)

echo.
echo [4/6] PyInstaller packing...
if exist "dist\sphgj" rmdir /S /Q "dist\sphgj"
python -m PyInstaller --noconsole --name sphgj --clean --noconfirm ^
    --add-data "frontend\dist;frontend\dist" ^
    --add-data "config.json;." ^
    --hidden-import "webview.platforms.edgechromium" ^
    --hidden-import "psutil" ^
    --collect-all webview ^
    --collect-all playwright ^
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
echo [5/6] Copy Playwright Chromium (match playwright version, slim)...
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
rem 只拷 playwright 期望版本的 chromium(不能取"最新",否则版本不匹配,exe 起不来)
set "REV_C="
set "REV_H="
for /f "tokens=1,2 delims=|" %%A in ('python tools\which_chromium.py 2^>nul') do (
    set "REV_C=%%A"
    set "REV_H=%%B"
)
if "!REV_C!"=="" (
    echo [Warn] Cannot detect expected chromium revision, fallback to latest dir.
    for /f "delims=" %%D in ('dir /ad /b /o-n "%SRC%\chromium-*" 2^>nul') do (
        set "REV_C=%%D" & goto :rev_c_done
    )
    :rev_c_done
    for /f "delims=" %%D in ('dir /ad /b /o-n "%SRC%\chromium_headless_shell-*" 2^>nul') do (
        set "REV_H=%%D" & goto :rev_h_done
    )
    :rev_h_done
) else (
    set "REV_C=chromium-!REV_C!"
    set "REV_H=chromium_headless_shell-!REV_H!"
)
if not exist "%SRC%\!REV_C!" (
    echo [Info] !REV_C! not found, installing playwright chromium...
    python -m playwright install chromium
    if errorlevel 1 (
        echo [Retry] Switching to China mirror...
        set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
        python -m playwright install chromium
    )
)
if not exist "%SRC%\!REV_C!" (
    echo [Error] chromium !REV_C! still missing.
    pause
    exit /b 1
)
echo [OK] Copy !REV_C!
xcopy /E /I /Y "%SRC%\!REV_C!" "%DST%\!REV_C!" >nul
if exist "%SRC%\!REV_H!" (
    echo [OK] Copy !REV_H!
    xcopy /E /I /Y "%SRC%\!REV_H!" "%DST%\!REV_H!" >nul
)
del /Q "%DST%\debug.log" 2>nul
echo [OK] Chromium copied to %DST%

echo.
echo [6/6] Prepare data dir...
if not exist "dist\sphgj\data" mkdir "dist\sphgj\data"
rem [分发脱敏] 绝不把账号配置打进 exe 包(_internal/config.json 里是真实账号)
if exist "dist\sphgj\_internal\config.json" del /Q "dist\sphgj\_internal\config.json"
if exist "dist\sphgj\config.json" del /Q "dist\sphgj\config.json"

echo.
echo ============================================
echo   Done!
echo   exe:    dist\sphgj\sphgj.exe
echo   browser: dist\sphgj\browsers\
echo   profiles\ and data\ created on first run.
echo ============================================
endlocal
pause
