# -*- coding: utf-8 -*-
"""rev59 补丁:main.py 退出/重启顺序 —— 清理由 lifespan shutdown 拥有,这里只等它。

改前(bug):
    [重启] 收到重启指令,正在优雅退出…
    WARNING graceful_shutdown 异常: Event loop is closed
    RuntimeWarning: coroutine 'AccountManager.stop_all' was never awaited
    -> 4 秒后 os._exit,Chromium 被硬砍。清理一次都没跑成。

改后:
    1) 先关窗口(前端页面卸载 -> WS 长连接自然断开,uvicorn 不用干等);
    2) 再 poke srv.should_exit,让 uvicorn 走 shutdown -> lifespan shutdown;
    3) 等 server.graceful_shutdown() 返回「清理真的完成」,而不是固定 sleep 几秒;
    4) 才 os._exit(超时时仍退出,但会在日志里留明确一行)。

附带:
    - uvicorn 加 timeout_graceful_shutdown,避免长连接把停机拖成无限等待
      (拖住就永远走不到 lifespan shutdown,清理又白费);
    - print 全部带时间戳 —— pythonw 下 stdout 重定向到 run.log,而 logging 的行
      有时间戳、print 的没有,混在一起看不出先后,重启期间尤其容易误判。

用法: python tools/patch_rev59_main.py
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "main.py")

PATCHES = []

# ---------------- 1. 常量 + 带时间戳的 print ----------------
PATCHES.append((
    '''# 软件版本(与 git tag revXX 对应)。rev12=CDP 捕获基线;rev13=主动轮询替代常驻响应监听 + 修复直播信号抖动;rev14=内存优化(browser headless 参数 + flv.js 缓冲上限)
VERSION = "14.0"
''',
    '''# 软件版本(与 git tag revXX 对应)。rev12=CDP 捕获基线;rev13=主动轮询替代常驻响应监听 + 修复直播信号抖动;rev14=内存优化(browser headless 参数 + flv.js 缓冲上限)
VERSION = "14.0"

# rev59: 退出/重启时的时间预算(秒)
#   DRAIN: uvicorn 等现有连接自己断开的耐心。拖太久就永远走不到 lifespan shutdown,
#          清理又白费;窗口已经先关掉了,WS 会立刻断,8s 足够。
#   GRACEFUL: 等「关浏览器 / 释放 profile 锁 / 关 playwright」完成的上限。
#          4 个带直播页的账号大约 3~8s,20s 是安全余量。
DRAIN_TIMEOUT_SEC = 8.0
GRACEFUL_TIMEOUT = 20.0


def _p(msg: str):
    """带时间戳的 print。

    pythonw 启动时 stdout/stderr 都被重定向进 run.log,而 logging 输出的每行自带
    时间戳、print 的没有 —— 混在一起时看不出先后。重启期间多进程交错写日志,
    没有时间戳的那几行很容易被误判成"乱序"。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
''',
    "常量 + _p()",
))

# ---------------- 2. uvicorn 加 drain 超时 ----------------
PATCHES.append((
    '''    config = uvicorn.Config(server.app, host="0.0.0.0", port=port,
                            log_level="warning", access_log=False)''',
    '''    config = uvicorn.Config(server.app, host="0.0.0.0", port=port,
                            log_level="warning", access_log=False,
                            # rev59: 给"等连接断开"设上限。无限等 => 走不到
                            # lifespan shutdown => 浏览器永远关不掉。
                            timeout_graceful_shutdown=DRAIN_TIMEOUT_SEC)''',
    "uvicorn timeout_graceful_shutdown",
))

# ---------------- 3. 退出钩子重写 ----------------
PATCHES.append((
    '''    _win = {"obj": None}

    def _shutdown_hook(delay: float):
        def _worker():
            time.sleep(float(delay))
            print("[重启] 收到重启指令,正在优雅退出…")
            try:
                server.graceful_shutdown()
            except Exception as e:
                print(f"[重启] graceful_shutdown 异常: {e}")
            try:
                srv.should_exit = True
            except Exception:
                pass
            try:
                if _win["obj"] is not None:
                    _win["obj"].destroy()      # 让 webview.start() 返回
            except Exception:
                pass
            time.sleep(4.0)                 # 给 uvicorn / 浏览器清理留时间
            os._exit(0)                     # 兜底:确保进程真的退出

        threading.Thread(target=_worker, daemon=True).start()
''',
    '''    _win = {"obj": None}

    def _stop_backend(timeout: float = GRACEFUL_TIMEOUT) -> bool:
        """让后端停机,并等后台清理【真的】完成。返回 True = 清理完成。

        rev59: 清理由 server 侧 FastAPI 的 lifespan shutdown 执行(在事件循环
        内部、循环关闭之前),这里只负责 poke + 等。以前是从工作线程往事件循环
        排 stop_all,循环一关就抛 RuntimeError,清理一次都没跑成。
        """
        try:
            srv.should_exit = True
        except Exception:
            pass
        try:
            return bool(server.graceful_shutdown(timeout=timeout))
        except Exception as e:
            _p(f"[退出] 等待清理异常: {e}")
            return False

    def _shutdown_hook(delay: float):
        def _worker():
            time.sleep(float(delay))
            _p("[重启] 收到重启指令,正在优雅退出…")
            # 1) 先关窗口:前端页面卸载 -> WS/长连接自然断开,uvicorn 才不用干等它们
            try:
                if _win["obj"] is not None:
                    _win["obj"].destroy()          # 让 webview.start() 返回
            except Exception as e:
                _p(f"[重启] 关闭窗口失败(忽略): {e}")
            # 2) 停机并等清理:关浏览器 / 释放 profile 锁 / 关 playwright
            ok = _stop_backend()
            if ok:
                _p("[重启] 清理完成,退出")
            else:
                _p(f"[重启] {GRACEFUL_TIMEOUT:.0f}s 内清理未完成,强制退出"
                   f"(浏览器可能残留,下次启动会自动清孤儿)")
            time.sleep(0.5)                        # 让 run.log 落盘
            os._exit(0 if ok else 3)

        threading.Thread(target=_worker, daemon=True).start()
''',
    "退出钩子:先关窗口 -> 停机 -> 等清理完成",
))

# ---------------- 4. 五处退出路径统一走 _stop_backend ----------------
PATCHES.append((
    '''        finally:
            server.graceful_shutdown()
            srv.should_exit = True
        return''',
    '''        finally:
            _stop_backend()
        return''',
    "headless Ctrl+C 退出",
))

PATCHES.append((
    '''        except KeyboardInterrupt:
            server.graceful_shutdown()
            srv.should_exit = True
        return''',
    '''        except KeyboardInterrupt:
            _stop_backend()
        return''',
    "DEV 浏览器模式退出",
))

PATCHES.append((
    '''    except ImportError:
        server.graceful_shutdown()
        srv.should_exit = True''',
    '''    except ImportError:
        _stop_backend()''',
    "缺 pywebview 退出",
))

PATCHES.append((
    '''        webview.start(gui=gui)
        # 窗口关闭 -> 优雅停止
        server.graceful_shutdown()
        srv.should_exit = True
    except Exception as e:
        server.graceful_shutdown()
        srv.should_exit = True''',
    '''        webview.start(gui=gui)
        # 窗口关闭(用户点 X,或重启钩子先 destroy 了窗口)-> 优雅停止
        # rev59: 这里必须等到清理完成才 return —— main() 一返回解释器就开始收尾,
        # daemon 线程会被直接掐掉,清理一半等于没清理。
        _stop_backend()
    except Exception as e:
        _stop_backend()''',
    "窗口关闭 / 异常退出",
))

# ---------------- 5. 其余 print 统一带时间戳 ----------------
PATCHES.append((
    '''    print(f"[ERROR] {msg}")''',
    '''    _p(f"[ERROR] {msg}")''',
    "_fatal 时间戳",
))
PATCHES.append((
    '''                        print(f"[兜底] 窗口处于隐藏状态,已强制显示 (hwnd={h})")''',
    '''                        _p(f"[兜底] 窗口处于隐藏状态,已强制显示 (hwnd={h})")''',
    "兜底显示时间戳",
))
PATCHES.append((
    '''            print(f"[兜底] 窗口可见性检查失败: {e}")''',
    '''            _p(f"[兜底] 窗口可见性检查失败: {e}")''',
    "兜底失败时间戳",
))
PATCHES.append((
    '''    print(f"[启动] 视频号工具 v{VERSION}" + (" (headless)" if headless else ""))''',
    '''    _p(f"[启动] 视频号工具 v{VERSION}" + (" (headless)" if headless else ""))''',
    "启动时间戳",
))
PATCHES.append((
    '''        print(f"[OK] headless 模式:后端已就绪 {url},Ctrl+C 或进程终止即退出")''',
    '''        _p(f"[OK] headless 模式:后端已就绪 {url},Ctrl+C 或进程终止即退出")''',
    "headless 就绪时间戳",
))
PATCHES.append((
    '''    print("[OK] 正在打开桌面窗口…")''',
    '''    _p("[OK] 正在打开桌面窗口…")''',
    "开窗口时间戳",
))
PATCHES.append((
    '''        print(f"[DEV] PINLUN_FORCE_BROWSER=1,用浏览器打开: {url}")
        print("[DEV] 关闭此窗口或按 Ctrl+C 退出")''',
    '''        _p(f"[DEV] PINLUN_FORCE_BROWSER=1,用浏览器打开: {url}")
        _p("[DEV] 关闭此窗口或按 Ctrl+C 退出")''',
    "DEV 提示时间戳",
))

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    n = s.count(old)
    assert n == 1, f"{tag}: 命中 {n} 次(期望 1)"
    s = s.replace(old, new, 1)
    print(f"ok  {tag}")

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------- 自校验 ----------------
s2 = io.open(P, encoding="utf-8").read()
checks = [
    ("_p 定义", "def _p(msg: str):" in s2),
    ("DRAIN_TIMEOUT_SEC", "DRAIN_TIMEOUT_SEC = 8.0" in s2),
    ("GRACEFUL_TIMEOUT", "GRACEFUL_TIMEOUT = 20.0" in s2),
    ("uvicorn drain 超时", "timeout_graceful_shutdown=DRAIN_TIMEOUT_SEC" in s2),
    ("_stop_backend 定义", "def _stop_backend(" in s2),
    ("钩子先停机后关窗口", s2.index("ok = _stop_backend()")
                                 < s2.index('_win["obj"].destroy()\n            except')),
    ("不再固定 sleep 4 秒强退", "time.sleep(4.0)                 # 给 uvicorn / 浏览器清理留时间" not in s2),
    ("旧的 graceful_shutdown 直呼已清理",
     "server.graceful_shutdown()\n" not in s2),
    ("print 只剩 _p 内部一处", s2.count("print(") == 1),
    ("窗口关闭路径等清理", "        _stop_backend()\n    except Exception as e:\n        _stop_backend()" in s2),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("\nSELFCHECK", "PASS" if not bad else f"FAIL {bad}")
