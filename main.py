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

# exe 内置浏览器定位
if getattr(sys, "frozen", False):
    base = os.path.dirname(sys.executable)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(base, "browsers"))
    # --noconsole 模式下 sys.stdout/stderr 为 None,uvicorn 日志 formatter 会调
    # sys.stdout.isatty() 报 AttributeError,print 也会写 None 崩溃。
    # 重定向到 exe 同级 pinlun.log,既避免崩溃也便于排查。
    if sys.stdout is None or sys.stderr is None:
        try:
            _logf = open(os.path.join(base, "pinlun.log"), "a", encoding="utf-8")
            if sys.stdout is None:
                sys.stdout = _logf
            if sys.stderr is None:
                sys.stderr = _logf
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


def main():
    import uvicorn
    from backend import server

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
