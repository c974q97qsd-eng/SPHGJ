"""视频号工具 - 入口。

启动 FastAPI(后台线程) + 打开 pywebview 桌面窗口指向本地服务。
窗口关闭后优雅停止 Playwright/账号管理。

exe 运行时自动定位内置 Playwright 浏览器(browsers/ 目录)。

**始终使用桌面窗口,不回退系统浏览器**:Windows 强制 WebView2(EdgeChromium)
原生窗口;Runtime 缺失或窗口创建失败时弹原生消息框提示安装并退出,
绝不降级为浏览器打开。仅当显式设置环境变量 PINLUN_FORCE_BROWSER=1
(供无图形服务器开发调试)时才回退浏览器。
"""
import os
import sys
import time
import socket
import threading
import urllib.request

# ⚠️ 必须最先执行: Electron 宿主(如 WorkBuddy)会向子进程注入
# CHROME_CRASHPAD_PIPE_NAME / ELECTRON_RUN_AS_NODE。WebView2 同为 Chromium,
# 继承前者会去连宿主的崩溃管道,启动即崩(弹窗 "Error launching
# CrashSender.exe");后者会让 Electron 子进程被当成纯 Node 运行。
# 从环境里剥掉,保证任何宿主/任何方式启动都正常。
for _k in ("CHROME_CRASHPAD_PIPE_NAME", "ELECTRON_RUN_AS_NODE"):
    os.environ.pop(_k, None)

# 软件版本(与 git tag revXX 对应)。rev12=CDP 捕获基线;rev13=主动轮询替代常驻响应监听 + 修复直播信号抖动;rev14=内存优化(browser headless 参数 + flv.js 缓冲上限)
VERSION = "14.0"

# exe 内置浏览器定位(打包模式)
if getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(base, "browsers"))
else:
    base = os.path.dirname(os.path.abspath(__file__))
    # 源码模式:优先使用随项目自带的离线浏览器(配置环境安装\ms-playwright),
    # 让所有启动方式(bat / vbs / 快捷方式 / 直接 python main.py)行为一致,
    # 不必依赖外部设置 PLAYWRIGHT_BROWSERS_PATH。已有外部设置时以外部为准。
    _offline_browsers = os.path.join(base, "配置环境安装", "ms-playwright")
    if os.path.isdir(_offline_browsers):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _offline_browsers)

# 无控制台模式(--noconsole 打包 / pythonw 启动)下 sys.stdout/stderr 为 None:
# uvicorn 日志会调 sys.stdout.isatty() 报 AttributeError,部分库写 None 也会崩。
# 重定向到 run.log 既避免崩溃,也保留崩溃后排查的线索。
# (界面上的「运行日志」板块由 backend/log_hub.py 提供,与此文件互补)
if sys.stdout is None or sys.stderr is None:
    try:
        _logf = open(os.path.join(base, "run.log"), "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf
        _logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        _logf.flush()
    except Exception:
        pass


def _free_port(preferred: int = 8712) -> int:
    for port in (preferred, 8713, 8714, 8715, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port if port else s.getsockname()[1]
            except OSError:
                continue
    return 0


def _wait_ready(port: int, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _fatal(msg: str):
    """桌面窗口无法创建时,弹原生消息框提示并退出(不回退浏览器)。"""
    print(f"[ERROR] {msg}")
    try:
        if _is_windows():
            import ctypes
            # MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(0, msg, "视频号工具 - 启动失败", 0x10)
        else:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("视频号工具 - 启动失败", msg)
            r.destroy()
    except Exception:
        pass
    if not getattr(sys, "frozen", False):
        try:
            input("按回车退出…")
        except Exception:
            pass


def _ensure_window_shown(title: str, wait: float = 25.0):
    """后台兜底:窗口被隐藏时强制显示。

    某些启动器(WScript.Shell.Run 传 0/SW_HIDE、快捷方式 ShowCommand=0)
    会让进程以「隐藏」方式启动,pywebview 的 WinForms 主窗口随之不可见 ——
    症状是后端正常、浏览器能打开页面、但看不到软件窗口,且没有任何报错。
    这里轮询一次:发现同名窗口处于隐藏态就强制 ShowWindow,避免这类
    「静默无窗口」故障。窗口本就可见时立即结束,无任何副作用。
    """
    if not _is_windows():
        return
    import ctypes
    from ctypes import wintypes

    def _worker():
        try:
            u32 = ctypes.windll.user32
            cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                         wintypes.LPARAM)
            hits = []

            def _cb(hwnd, _lp):
                n = u32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value == title:
                        hits.append(hwnd)
                return True

            cb = cb_type(_cb)
            deadline = time.time() + wait
            while time.time() < deadline:
                del hits[:]
                u32.EnumWindows(cb, 0)
                for h in hits:
                    if not u32.IsWindowVisible(h):
                        u32.ShowWindow(h, 5)          # SW_SHOW
                        u32.SetForegroundWindow(h)
                        print(f"[兜底] 窗口处于隐藏状态,已强制显示 (hwnd={h})")
                    return                            # 已可见或已处理
                time.sleep(0.5)
        except Exception as e:
            print(f"[兜底] 窗口可见性检查失败: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def main():
    # --headless: 只启动后端服务,不创建桌面窗口。
    # 用途: AI/命令行环境下做内存诊断、数据采集等后台任务。
    # pywebview(WebView2) 在非用户交互会话的进程树里启动会崩溃
    # (msedgewebview2 报 "Error launching CrashSender.exe"),窗口必须由
    # 用户双击 VBS/BAT 启动;headless 模式不创建窗口,不受此限制。
    headless = "--headless" in sys.argv

    import uvicorn
    from backend import server

    print(f"[启动] 视频号工具 v{VERSION}" + (" (headless)" if headless else ""))

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    port = _free_port()
    # 绑 0.0.0.0:桌面窗口仍走 127.0.0.1 本机访问,同时允许内网浏览器访问
    config = uvicorn.Config(server.app, host="0.0.0.0", port=port,
                            log_level="warning", access_log=False)
    srv = uvicorn.Server(config)

    # 后台线程跑 uvicorn
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()

    if not _wait_ready(port):
        _fatal("后端服务启动失败,请查看日志后重试。")
        return

    url = f"http://127.0.0.1:{port}/"
    if headless:
        print(f"[OK] headless 模式:后端已就绪 {url},Ctrl+C 或进程终止即退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.graceful_shutdown()
            srv.should_exit = True
        return

    print("[OK] 正在打开桌面窗口…")

    # 仅无图形服务器开发调试时允许回退浏览器(显式开关,默认关闭)
    force_browser = os.environ.get("PINLUN_FORCE_BROWSER") == "1"

    if force_browser:
        print(f"[DEV] PINLUN_FORCE_BROWSER=1,用浏览器打开: {url}")
        print("[DEV] 关闭此窗口或按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.graceful_shutdown()
            srv.should_exit = True
        return

    # 始终使用桌面窗口;Windows 强制 WebView2 原生窗口
    try:
        import webview
    except ImportError:
        server.graceful_shutdown()
        srv.should_exit = True
        _fatal(
            "缺少桌面窗口依赖 pywebview,无法打开窗口。\n\n"
            "请先双击 setup.bat 安装依赖,或手动执行:\n"
            "    pip install pywebview\n\n"
            "本软件仅以桌面窗口运行,不支持用浏览器打开。"
        )
        return

    try:
        gui = "edgechromium" if _is_windows() else None
        webview.create_window("视频号工具", url, width=1280, height=820,
                              min_size=(900, 600))
        _ensure_window_shown("视频号工具")
        webview.start(gui=gui)
        # 窗口关闭 -> 优雅停止
        server.graceful_shutdown()
        srv.should_exit = True
    except Exception as e:
        server.graceful_shutdown()
        srv.should_exit = True
        tip = (
            "无法创建桌面窗口。\n\n"
            f"原因: {e}\n\n"
            "Windows 请确保已安装「WebView2 Runtime」(Win10/11 通常已自带);\n"
            "缺失可运行 setup.bat 自动安装,或访问:\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
            "本软件仅以桌面窗口运行,不支持用浏览器打开。"
        )
        _fatal(tip)


if __name__ == "__main__":
    main()
