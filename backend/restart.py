"""进程重启通道 —— 让桌面程序能被远程重启。

为什么需要:
    改代码或 config.json 后必须重启才生效,而运行机常常不在这台电脑上。
    有了本模块,内网任意机器执行

        POST http://<运行机>:8712/api/system/restart

    就能让它「优雅退出 + 按原方式重新拉起」,不必再跑到那台机器上双击。

实现要点(都是踩过的坑):
    1) 不能「父进程 kill 自己再拉起」:父进程一死,仍在同一进程树里的 helper
       会被 taskkill /T 连带清掉,结果就是「关了但没起来」。helper 必须用
       DETACHED_PROCESS 启动,彻底脱离进程树。
    2) helper 脚本不能放 tools/ —— exe 打包不会带 tools/。故源码以字符串常量
       内嵌在本文件,运行时落盘到 data/restart_helper.py 再执行。
    3) 新实例的启动方式必须与当前完全一致(解释器/参数/工作目录/环境变量),
       所以主程序启动时把「怎么启动我」写进 data/launch.json。
       注意运行机是用 start-noconsole.vbs 起的 pythonw.exe,不是 python.exe。
    4) pythonw.exe 属于 GUI 子系统,没有 stdout —— helper 与新实例的日志一律
       重定向到文件,绝不依赖控制台输出。
    5) 退出要优雅:先 graceful_shutdown(关 Playwright / 浏览器 / 释放 profile 锁),
       再 destroy 窗口让 webview.start() 返回,最后 os._exit 兜底。
"""
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

logger = logging.getLogger("sphgj")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DATA_DIR = os.path.join(ROOT, "data")
LAUNCH_PATH = os.path.join(DATA_DIR, "launch.json")
STATUS_PATH = os.path.join(DATA_DIR, "restart_status.json")
REQUEST_PATH = os.path.join(DATA_DIR, "restart_request.json")
HELPER_PATH = os.path.join(DATA_DIR, "restart_helper.py")
HELPER_LOG = os.path.join(DATA_DIR, "restart_helper.log")

DEFAULT_PORTS = [8712, 8713, 8714, 8715]

# 主程序在启动时调用 register_exit_hook 注册「怎么退出」;本模块不直接 import
# main,避免循环依赖(main -> server -> restart)。
_exit_hook = None
_runtime = {"version": "", "headless": False, "port": 0, "started_at": ""}


# ---------------------------------------------------------------- 运行信息

def set_runtime_info(version=None, headless=None, port=None):
    """由 main.py 在启动时上报运行信息。"""
    if version is not None:
        _runtime["version"] = str(version)
    if headless is not None:
        _runtime["headless"] = bool(headless)
    if port is not None:
        _runtime["port"] = int(port)
    if not _runtime.get("started_at"):
        _runtime["started_at"] = datetime.now().isoformat(timespec="seconds")


def runtime_info():
    """当前进程的运行信息 —— 远程重启后靠 pid / started_at 变化判定成功。"""
    info = dict(_runtime)
    info["pid"] = os.getpid()
    info["root"] = ROOT
    info["frozen"] = bool(getattr(sys, "frozen", False))
    info["executable"] = sys.executable
    return info


# ---------------------------------------------------------------- launch.json

def build_launch_cmd():
    """还原「当前进程是怎么被启动的」命令行。"""
    if getattr(sys, "frozen", False):
        # 打包模式:sys.executable 就是 exe,argv[0] 也是 exe
        return [sys.executable] + list(sys.argv[1:])
    # 源码模式:argv[0] 是 main.py 的路径(python main.py / pythonw main.py)
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else \
        os.path.join(ROOT, "main.py")
    return [sys.executable, script] + list(sys.argv[1:])


def write_launch_info():
    """把「怎么启动我」落盘,供重启时原样复现。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    info = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "frozen": bool(getattr(sys, "frozen", False)),
        "headless": bool(_runtime.get("headless")),
        "version": _runtime.get("version") or "",
        "port": _runtime.get("port") or 0,
        "cmd": build_launch_cmd(),
        "cwd": ROOT,
        "env": {"PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")},
    }
    _atomic_json(LAUNCH_PATH, info)
    logger.info(f"[restart] 已记录启动信息: {info['cmd']}")
    return info


def read_launch_info():
    try:
        with open(LAUNCH_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_restart_status():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 退出钩子

def register_exit_hook(fn):
    """注册「安排退出」的回调:fn(delay_seconds) -> None。"""
    global _exit_hook
    _exit_hook = fn


def request_exit(delay=3.0):
    if _exit_hook is None:
        logger.error("[restart] 未注册退出钩子,无法重启(可能不是通过 main.py 启动)")
        return False
    _exit_hook(float(delay))
    return True


# ---------------------------------------------------------------- 重启

def spawn_helper(delay=3.0, ports=None):
    """拉起脱离进程树的重启助手,返回其 pid。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    launch = read_launch_info()
    if not launch or not launch.get("cmd"):
        # 没记录过启动信息(例如手工 import server 跑测试):现场补记一份
        write_launch_info()
        launch = read_launch_info()
    if not launch or not launch.get("cmd"):
        raise RuntimeError("缺少 data/launch.json(启动信息),无法自动重启")

    with open(HELPER_PATH, "w", encoding="utf-8") as f:
        f.write(HELPER_SOURCE)

    req = {
        "old_pid": os.getpid(),
        "delay": float(delay),
        "cmd": launch["cmd"],
        "cwd": launch.get("cwd") or ROOT,
        "ports": list(ports or DEFAULT_PORTS),
        "env": launch.get("env") or {},
        "log": os.path.join(ROOT, "run.log"),
        "status": STATUS_PATH,
        "helper_log": HELPER_LOG,
    }
    _atomic_json(REQUEST_PATH, req)

    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    # 某些启动方式(IDE / 沙箱 / 任务计划)会把本进程放进 Job Object,Job 关闭时
    # 会连带结束所有子进程 —— 那样 helper 还没来得及接管就被清掉了。带
    # CREATE_BREAKAWAY_FROM_JOB 脱离;若该 Job 不允许 breakaway 会报错,退回即可
    # (正常由用户双击启动时本就不在 Job 里,退回后一样能工作)。
    BREAKAWAY = 0x01000000
    logf = open(HELPER_LOG, "a", encoding="utf-8")
    logf.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] "
               f"拉起 helper old_pid={os.getpid()} delay={delay}s cmd={launch['cmd']}\n")
    logf.flush()

    def _spawn(extra_flags):
        return subprocess.Popen([sys.executable, HELPER_PATH, REQUEST_PATH],
                                cwd=ROOT, close_fds=True,
                                creationflags=flags | extra_flags,
                                stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=subprocess.STDOUT)

    try:
        p = _spawn(BREAKAWAY)
    except OSError as e:
        logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] "
                   f"breakaway 失败({e}),退回普通 detached\n")
        logf.flush()
        p = _spawn(0)
    logger.info(f"[restart] helper 已启动 pid={p.pid},将在 {delay}s 后接管")
    return p.pid


# rev59: 同一个进程只允许安排一次重启。
# 2 分钟内连点 3 次会起 3 个 helper,每个都"等旧进程死 -> 拉新实例",
# 极端情况下同时出现两个新实例(一个占 8712、一个退到 8713),互相抢 profile 锁。
_inflight = {"at": 0.0, "helper_pid": 0}
INFLIGHT_WINDOW = 120.0


def restart(delay=3.0, ports=None):
    """安排一次重启:先起 helper,再让本进程优雅退出。

    若 INFLIGHT_WINDOW 秒内已经安排过一次,返回 {"already": True, ...} 且不起新 helper
    (重复点重启不该变成"重启两次")。
    """
    delay = max(1.0, min(float(delay), 60.0))
    since = time.time() - _inflight["at"]
    if _inflight["at"] and since < INFLIGHT_WINDOW:
        logger.info(f"[restart] {since:.0f}s 前已安排过重启"
                    f"(helper_pid={_inflight['helper_pid']}),忽略重复请求")
        return {"old_pid": os.getpid(), "helper_pid": _inflight["helper_pid"],
                "delay": delay, "already": True, "since": round(since, 1)}
    helper_pid = spawn_helper(delay, ports=ports)
    _inflight.update(at=time.time(), helper_pid=helper_pid)
    _atomic_json(STATUS_PATH, {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ok": None, "phase": "pending",
        "old_pid": os.getpid(), "helper_pid": helper_pid,
        "new_pid": 0, "port": 0, "delay": delay, "error": "",
    })
    if not request_exit(delay):
        raise RuntimeError("退出钩子未注册:请以 main.py 启动(而非直接跑 server)")
    return {"old_pid": os.getpid(), "helper_pid": helper_pid, "delay": delay}


# ---------------------------------------------------------------- helper 源码

HELPER_SOURCE = r'''"""重启助手(由 backend/restart.py 自动生成,勿手动编辑)。

职责:等旧进程退出 -> 必要时强杀 -> 等端口释放 -> 按原样拉起新实例 -> 回写结果。
本进程由主程序以 DETACHED_PROCESS 启动,不在旧进程的进程树里,
因此不会被 taskkill /T 连带清掉。
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
PROCESS_TERMINATE = 0x0001
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
BREAKAWAY_FROM_JOB = 0x01000000


def log(req, msg):
    try:
        with open(req["helper_log"], "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def pid_alive(pid):
    """不依赖 tasklist / PATH:直接问内核。"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k.CloseHandle(h)


def port_open(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def kill_pid(pid):
    """只杀这一个进程,不带 /T —— helper 是 detached 的,带 /T 有被连带的风险。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    if pid_alive(pid):
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if h:
            k.TerminateProcess(h, 1)
            k.CloseHandle(h)


def write_status(req, **kw):
    d = {"at": time.strftime("%Y-%m-%d %H:%M:%S")}
    d.update(kw)
    try:
        tmp = req["status"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, req["status"])
    except Exception:
        pass


def running_pid(ports):
    """探一遍端口,返回 (端口, pid):第一个能上报 /api/system/info 的实例。

    拉新实例之前必须先问一句 —— 并发重启或用户手动双击启动时,
    外面可能已经有一个新实例在跑,再拉一个就是两个实例抢同一批 profile。
    """
    import urllib.request
    for p in ports:
        if not port_open(p):
            continue
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/system/info" % int(p), timeout=2) as r:
                if r.status != 200:
                    continue
                d = json.loads(r.read().decode("utf-8", "replace"))
                if d.get("pid"):
                    return int(p), int(d["pid"])
        except Exception:
            pass
    return 0, 0


def wait_api(ports, timeout, old_pid):
    """等【新实例】就绪,返回端口(0 = 超时)。

    必须校验 pid 不是旧的 —— 否则旧进程没死透时,探到它的端口会误报成功。
    """
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in ports:
            if not port_open(p):
                continue
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/system/info" % int(p), timeout=2) as r:
                    if r.status != 200:
                        continue
                    d = json.loads(r.read().decode("utf-8", "replace"))
                    if d.get("pid") and int(d["pid"]) != int(old_pid):
                        return int(p)
            except Exception:
                pass
        time.sleep(0.5)
    return 0


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        req = json.load(f)
    old_pid = req["old_pid"]
    ports = [int(x) for x in (req.get("ports") or [8712, 8713, 8714, 8715])]
    log(req, "helper 启动,%.1fs 后接管 (old_pid=%s)" % (req.get("delay", 3), old_pid))

    time.sleep(float(req.get("delay", 3)))

    # 1) 等旧进程自然退出(主程序会优雅关闭浏览器,可能要十几秒)
    # rev59: 旧进程现在会「排空连接(<=8s)+ 关浏览器(<=20s)」才退出,45s 太紧,
    # 紧到会误杀正在收尾的进程 —— 那就又变成硬砍 Chromium 了。
    deadline = time.time() + 75
    while time.time() < deadline and pid_alive(old_pid):
        time.sleep(0.5)
    if pid_alive(old_pid):
        log(req, "旧进程 %s 仍在,强制结束" % old_pid)
        kill_pid(old_pid)
        time.sleep(1.0)

    # 1.5) 已经有别的新实例在跑?认领它,别再造一个(rev59)
    port0, pid0 = running_pid(ports)
    if pid0 and pid0 != old_pid:
        log(req, "已有实例在跑(pid=%s, 端口 %s),跳过拉起" % (pid0, port0))
        write_status(req, ok=True, phase="done", old_pid=old_pid,
                     new_pid=pid0, port=port0, error="")
        return 0

    # 2) 等端口释放
    deadline = time.time() + 20
    while time.time() < deadline and any(port_open(p) for p in ports):
        time.sleep(0.5)

    # 3) 拉起新实例(失败重试一次)
    for attempt in (1, 2):
        env = os.environ.copy()
        for k, v in (req.get("env") or {}).items():
            if v:
                env[k] = v
        try:
            logf = open(req["log"], "a", encoding="utf-8")
        except Exception as e:
            write_status(req, ok=False, phase="failed", old_pid=old_pid, new_pid=0,
                         port=0, error="打开日志失败: %r" % (e,))
            return 1
        base_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        p = None
        last_err = None
        for extra in (BREAKAWAY_FROM_JOB, 0):
            try:
                p = subprocess.Popen(req["cmd"], cwd=req.get("cwd") or None, env=env,
                                     close_fds=True, stdin=subprocess.DEVNULL,
                                     stdout=logf, stderr=subprocess.STDOUT,
                                     creationflags=base_flags | extra)
                break
            except Exception as e:
                last_err = e
        if p is None:
            log(req, "启动失败(第 %d 次): %r" % (attempt, last_err))
            write_status(req, ok=False, phase="failed", old_pid=old_pid, new_pid=0,
                         port=0, error="启动命令失败: %r" % (last_err,))
            return 1
        log(req, "已拉起新实例 pid=%s (第 %d 次),等待服务就绪" % (p.pid, attempt))
        port = wait_api(ports, 90, old_pid)
        if port:
            log(req, "服务已就绪,端口 %d" % port)
            write_status(req, ok=True, phase="done", old_pid=old_pid,
                         new_pid=p.pid, port=port, error="")
            return 0
        log(req, "90s 内未就绪,清理后重试")
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(2)

    write_status(req, ok=False, phase="failed", old_pid=old_pid, new_pid=0, port=0,
                 error="新实例启动后 90s 内服务未就绪")
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''
