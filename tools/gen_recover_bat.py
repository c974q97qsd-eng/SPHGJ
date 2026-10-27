"""生成「恢复启动.bat」—— 程序异常退出 / 重启失败时的一键自救脚本。

背景（2026-10-27）：
    远程重启时 helper 在 Python 解释器 core init 阶段就挂了：
        Fatal Python error: init_sys_streams ... MemoryError
    旧进程随后退出 -> 服务彻底没了（8712-8715 全无监听）。
    根因是母本内存被吃满 —— run.log 里 21:45 就已经出现 Playwright 连接层
    MemoryError，之后 dashboard 抓取全线失败。

    这种情况下现场往往还有 Playwright 的浏览器 / node 驱动残留占着内存，
    所以恢复脚本第一件事就是清残留，然后才启动。

写法约束（和另外三个 bat 一致）：
    - GBK 编码 + 全 CRLF（cmd 对 LF-only 的 bat 解析会错行，实测过）
    - echo 行里不许有半角括号（cmd 在 if 块内会把它当块结束）
    - **外部命令一律用 %SystemRoot% 下的绝对路径** —— 不依赖 PATH。
      本机实测：PATH 里被 msys 抢先时，`timeout` 会命中 Unix 版、
      `powershell` / `wmic` 直接"不是内部或外部命令"。
    - PowerShell 的 `|` `(` `)` 在 cmd 双引号内不需要 `^` 转义；
      加了反而会把 `^` 传给 PowerShell 造成语法错误。
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

RECOVER = r'''@echo off
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
'''


def tame_echo(text):
    """echo 行里的半角括号换全角。

    cmd 在 `if ... ( ... )` 块内遇到未转义的 `)` 会提前结束块。这里只在
    echo 行上做替换，且跳过 PowerShell 那种整行含 `^` 的（本来就不该出现在
    echo 里，出现说明写错了）。
    """
    out = []
    for line in text.split("\n"):
        st = line.strip().lower()
        if st.startswith("echo") and "\x5e" not in line and ("(" in line or ")" in line):
            head, sep, body = line.partition("echo")
            body = body.replace("(", "（").replace(")", "）")
            line = head + sep + body
        out.append(line)
    return "\n".join(out)


def write_gbk(name, text):
    text = tame_echo(text).replace("\r\n", "\n").replace("\n", "\r\n")
    p = os.path.join(ROOT, name)
    with open(p, "wb") as f:
        f.write(text.encode("gbk"))
    return p


def main():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            ok = False
        print(("  PASS  " if cond else "  FAIL  ") + msg)

    name = "恢复启动.bat"
    p = write_gbk(name, RECOVER)
    raw = open(p, "rb").read()
    txt = raw.decode("gbk")
    lines = txt.splitlines()

    chk(True, "%s 已生成 (%d 字节 / %d 行)" % (name, len(raw), len(lines)))
    chk(b"\n" not in raw.replace(b"\r\n", b""), "全部 CRLF（无裸 LF）")

    bad = [ln for ln in lines
           if ln.strip().lower().startswith("echo") and ("(" in ln or ")" in ln)]
    chk(not bad, "echo 行无半角括号" + ("" if not bad else " -> %s" % bad[:2]))

    chk("%SystemRoot%\\System32" in txt, "外部命令走 System32 绝对路径")
    chk(txt.count("%PS%") >= 2, "PowerShell 用变量引用")
    chk("ms-playwright" in txt and "playwright" in txt, "含残留进程清理")
    chk("ping.exe" in txt and "timeout" not in txt.split(":launch")[0].lower().split("chcp")[-1].replace("timeout:", ""),
        "不用 timeout（本机 PATH 会命中 Unix 版）")
    chk("MemoryError" in txt, "含失败提示文案")

    print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
