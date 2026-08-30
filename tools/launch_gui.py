# -*- coding: utf-8 -*-
"""干净启动 SPHGJ GUI（供 AI 助手调用，等同用户双击 BAT/VBS）。

机制:
- CREATE_BREAKAWAY_FROM_JOB: 子进程脱离调用方的作业对象(WorkBuddy 的 job
  会限制子进程,WebView2 在其中启动必崩)。
- DETACHED_PROCESS: 无控制台窗口。
- 环境变量剥掉 CHROME_*/ELECTRON_*: Electron 注入的崩溃管道变量会干扰
  WebView2 的 crashpad。
"""
import os
import subprocess
import sys

PROJ = r"D:\SPHGJ"
PYW = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Python", "pythoncore-3.14-64", "pythonw.exe"
)
if not os.path.exists(PYW):
    sys.exit(f"pythonw 不存在: {PYW}")

env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CHROME_", "ELECTRON_"))}

def _spawn(flags):
    return subprocess.Popen(
        [PYW, "main.py"],
        cwd=PROJ,
        env=env,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

try:
    p = _spawn(
        subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )
except PermissionError:
    # 作业对象不允许 breakaway 时降级(仍为 detached + 干净环境)
    p = _spawn(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
print(f"launched pid={p.pid}")
