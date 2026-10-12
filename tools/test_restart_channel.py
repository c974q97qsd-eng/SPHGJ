# -*- coding: utf-8 -*-
"""远程重启通道 回归测试(不真的重启任何进程:构造检查 + 真内核调用小验证)。

为什么要这个通道:
  改代码/config.json 后必须重启才生效,而运行机常常不在这台电脑上。
  所以留一个 POST /api/system/restart,让它「优雅退出 + 按原方式重新拉起」。

关键约束(改动前先读,别改回去):
  a) helper 必须 DETACHED_PROCESS 启动 —— 否则父进程一死,它会被 taskkill /T
     连带清掉,结果是「关了但没起来」;
  b) helper 只 kill 旧 pid 本身(不带 /T),避免误伤自己;
  c) 新实例的就绪探测必须校验 pid != 旧 pid —— 否则旧进程没死透时会探到它的端口
     并误报重启成功;
  d) 新实例的命令行必须与当前完全一致:运行机是 pythonw.exe main.py,
     不是 python.exe,也不能丢 --headless;
  e) pythonw.exe 没有 stdout,helper 与新实例的日志一律重定向到文件,不许 print。

跑法: 配置环境安装\\Python314\\python.exe tools/test_restart_channel.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backend.restart as rs                  # noqa: E402

PASS = 0
FAIL = 0


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


tmp = tempfile.mkdtemp(prefix="sphgj_rst_")
_saved = (rs.DATA_DIR, rs.LAUNCH_PATH, rs.STATUS_PATH, rs.REQUEST_PATH,
          rs.HELPER_PATH, rs.HELPER_LOG, dict(rs._runtime), rs._exit_hook)
rs.DATA_DIR = tmp
rs.LAUNCH_PATH = os.path.join(tmp, "launch.json")
rs.STATUS_PATH = os.path.join(tmp, "restart_status.json")
rs.REQUEST_PATH = os.path.join(tmp, "restart_request.json")
rs.HELPER_PATH = os.path.join(tmp, "restart_helper.py")
rs.HELPER_LOG = os.path.join(tmp, "restart_helper.log")

print("=" * 72)
print("A. 命令行还原(build_launch_cmd)")
print("=" * 72)

cmd = rs.build_launch_cmd()
chk("第一个元素是解释器(当前 sys.executable)", cmd[0] == sys.executable, str(cmd[:1]))
chk("第二个元素是入口脚本的绝对路径",
    len(cmd) > 1 and os.path.isabs(cmd[1]), str(cmd))
chk("第二个元素 = 当前 sys.argv[0] 的绝对路径",
    len(cmd) > 1 and cmd[1] == os.path.abspath(sys.argv[0]), str(cmd[:2]))
chk("沿用当前 sys.argv 的附加参数",
    cmd[2:] == list(sys.argv[1:]), str(cmd[2:]))

# 真实场景:运行机是 pythonw main.py 起的(必要时带 --headless)
_sv_argv = sys.argv
try:
    sys.argv = [os.path.join(ROOT, "main.py"), "--headless"]
    c2 = rs.build_launch_cmd()
finally:
    sys.argv = _sv_argv
chk("模拟 pythonw main.py --headless:还原成 [exe, <ROOT>/main.py, --headless]",
    c2 == [sys.executable, os.path.join(ROOT, "main.py"), "--headless"], str(c2))
chk("--headless 不会被丢掉(否则重启后会凭空冒出窗口)",
    "--headless" in c2)

print()
print("=" * 72)
print("B. launch.json 往返 / 状态文件容错")
print("=" * 72)

rs.set_runtime_info(version="14.0", headless=False, port=8712)
info = rs.write_launch_info()
chk("write_launch_info 落盘成功", os.path.exists(rs.LAUNCH_PATH))
back = rs.read_launch_info()
chk("读回 cmd 与写入一致", back and back["cmd"] == info["cmd"], str(back and back["cmd"]))
chk("读回 cwd = 项目根目录", back and back["cwd"] == ROOT, str(back and back["cwd"]))
chk("记录了 pid", back and back["pid"] == os.getpid(), str(back and back["pid"]))
chk("记录了 frozen=False(源码模式)", back and back["frozen"] is False)
chk("记录了 headless=False", back and back["headless"] is False)
chk("记录了 playright 浏览器目录(可为空字符串)",
    back is not None and "PLAYWRIGHT_BROWSERS_PATH" in (back.get("env") or {}),
    str(back and back.get("env")))
chk("version 已写入", back and back["version"] == "14.0")

_save_launch, rs.LAUNCH_PATH = rs.LAUNCH_PATH, os.path.join(tmp, "no_such_launch.json")
chk("read_launch_info 对不存在的文件返回 None(不抛)", rs.read_launch_info() is None)
rs.LAUNCH_PATH = _save_launch

_save_status, rs.STATUS_PATH = rs.STATUS_PATH, os.path.join(tmp, "no_such.json")
chk("read_restart_status 对不存在的文件返回 None", rs.read_restart_status() is None)
rs.STATUS_PATH = _save_status

print()
print("=" * 72)
print("C. helper 源码(内嵌字符串)")
print("=" * 72)

try:
    compile(rs.HELPER_SOURCE, "<helper>", "exec")
    chk("HELPER_SOURCE 语法正确", True)
except SyntaxError as e:
    chk("HELPER_SOURCE 语法正确", False, repr(e))

ns = {"__name__": "helper_under_test"}
exec(compile(rs.HELPER_SOURCE, "<helper>", "exec"), ns)

for fn in ("log", "pid_alive", "port_open", "kill_pid", "write_status",
           "wait_api", "main"):
    chk("helper 定义了 %s()" % fn, callable(ns.get(fn)))

chk("helper 没有任何 print( pythonw 下 sys.stdout 为 None,print 会崩 )",
    "print(" not in rs.HELPER_SOURCE)
chk("helper 以 DETACHED_PROCESS 派生子进程",
    "DETACHED_PROCESS" in rs.HELPER_SOURCE
    and "base_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP" in rs.HELPER_SOURCE)
chk("helper 派生新实例时也尝试脱离 Job Object",
    "for extra in (BREAKAWAY_FROM_JOB, 0):" in rs.HELPER_SOURCE)
chk("helper 就绪探测校验 pid 不是旧的",
    "!= int(old_pid)" in rs.HELPER_SOURCE)
chk("helper 强杀旧进程时不带 /T(避免误伤 detached 的自己)",
    '["taskkill", "/PID", str(pid), "/F"]' in rs.HELPER_SOURCE)
chk("helper 只在清理自己启动失败的实例时才用 /T",
    '["taskkill", "/PID", str(p.pid), "/T", "/F"]' in rs.HELPER_SOURCE)
chk("helper 日志走文件重定向",
    "stdout=logf, stderr=subprocess.STDOUT" in rs.HELPER_SOURCE)
chk("helper 有启动失败重试", "for attempt in (1, 2):" in rs.HELPER_SOURCE)

# 真内核调用(快,无副作用)
chk("pid_alive(自己) = True", ns["pid_alive"](os.getpid()) is True)
chk("pid_alive(不存在的 pid) = False", ns["pid_alive"](999999) is False)
chk("port_open(未监听端口) = False", ns["port_open"](59999) is False)

print()
print("=" * 72)
print("D. 运行信息 / 退出钩子")
print("=" * 72)

ri = rs.runtime_info()
for k in ("pid", "root", "frozen", "executable", "version", "headless",
          "port", "started_at"):
    chk("runtime_info 含 %s" % k, k in ri, str(sorted(ri.keys())))
chk("runtime_info.pid = 当前进程", ri["pid"] == os.getpid())
chk("runtime_info.root = 项目根目录", ri["root"] == ROOT)
chk("runtime_info.started_at 非空", bool(ri["started_at"]))

rs._exit_hook = None
chk("未注册钩子时 request_exit 返回 False(不假装成功)",
    rs.request_exit(1) is False)

seen = []
rs.register_exit_hook(lambda d: seen.append(d))
chk("注册后 request_exit 返回 True", rs.request_exit(2.5) is True)
chk("钩子收到了 delay", seen == [2.5], str(seen))

print()
print("=" * 72)
print("E. spawn_helper(只构造,不真启动)")
print("=" * 72)

calls = []


class FakePopen:
    def __init__(self, args, **kw):
        calls.append((args, kw))
        self.pid = 424242
        self.args = args


_real_popen = rs.subprocess.Popen
rs.subprocess.Popen = FakePopen
try:
    helper_pid = rs.spawn_helper(delay=3, ports=[8712])
finally:
    rs.subprocess.Popen = _real_popen

chk("helper 用当前解释器启动",
    calls and calls[0][0][0] == sys.executable, str(calls and calls[0][0][:2]))
chk("helper 参数是落盘脚本 + 请求文件",
    calls and calls[0][0][1] == rs.HELPER_PATH and calls[0][0][2] == rs.REQUEST_PATH,
    str(calls and calls[0][0][1:]))
kw = calls[0][1] if calls else {}
flags = kw.get("creationflags", 0)
chk("DETACHED_PROCESS 已设置(0x8)", bool(flags & 0x8), hex(flags))
chk("CREATE_NEW_PROCESS_GROUP 已设置(0x200)", bool(flags & 0x200), hex(flags))
chk("首次尝试带 CREATE_BREAKAWAY_FROM_JOB(0x01000000)",
    bool(flags & 0x01000000), hex(flags))
chk("源码里有 breakaway 失败退回逻辑", "except OSError as e:" in
    io.open(os.path.join(ROOT, "backend", "restart.py"), encoding="utf-8").read())
chk("close_fds=True", kw.get("close_fds") is True)
chk("stdin 为 DEVNULL", kw.get("stdin") == rs.subprocess.DEVNULL)
chk("stdout/stderr 指向日志文件",
    hasattr(kw.get("stdout"), "write") and kw.get("stderr") == rs.subprocess.STDOUT)
chk("spawn_helper 返回 helper 的 pid", helper_pid == 424242, str(helper_pid))

req = json.load(io.open(rs.REQUEST_PATH, encoding="utf-8"))
for k in ("old_pid", "delay", "cmd", "cwd", "ports", "env", "log",
          "status", "helper_log"):
    chk("请求文件含 %s" % k, k in req, str(sorted(req.keys())))
chk("old_pid = 当前进程", req["old_pid"] == os.getpid())
chk("delay 已传入", req["delay"] == 3.0, str(req["delay"]))
chk("ports 已传入", req["ports"] == [8712], str(req["ports"]))
chk("命令与 launch.json 一致", req["cmd"] == back["cmd"])
chk("helper 脚本已落盘", os.path.exists(rs.HELPER_PATH))

print()
print("=" * 72)
print("F. 服务端路由(静态检查 server.py)")
print("=" * 72)

srv = io.open(os.path.join(ROOT, "backend", "server.py"), encoding="utf-8").read()
i_info = srv.find('@app.get("/api/system/info")')
i_restart = srv.find('@app.post("/api/system/restart")')
i_catchall = srv.find('"/{full_path:path}"')

chk("有 GET /api/system/info", i_info > 0)
chk("有 POST /api/system/restart", i_restart > 0)
chk("两个路由都注册在 catch-all 之前(否则会被静态路由吃掉)",
    0 < i_info < i_catchall and 0 < i_restart < i_catchall,
    "info=%d restart=%d catchall=%d" % (i_info, i_restart, i_catchall))
chk("info 返回 runtime_info", 'restart_mod.runtime_info()' in srv)
chk("info 返回上次重启结果", "restart_mod.read_restart_status()" in srv)
chk("restart 调 restart_mod.restart", "restart_mod.restart(delay)" in srv)
chk("restart 的 delay 有上下界约束", "Query(3.0, ge=1, le=60)" in srv)
chk("restart 失败会转成 HTTP 500", "重启失败" in srv)
chk("已 import restart 模块", "from . import restart as restart_mod" in srv)

print()
print("=" * 72)
print("G. 入口接线(静态检查 main.py)")
print("=" * 72)

mp = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
chk("main.py 上报运行信息", "restart_mod.set_runtime_info(" in mp)
chk("main.py 记录启动信息", "restart_mod.write_launch_info()" in mp)
chk("main.py 注册退出钩子", "restart_mod.register_exit_hook(" in mp)
_sh = mp.split("def _shutdown_hook")[1] if "def _shutdown_hook" in mp else ""
chk("main.py 有退出钩子实现", "def _shutdown_hook" in mp)
chk("退出钩子先优雅关浏览器", "server.graceful_shutdown()" in _sh)
chk("退出钩子会销毁窗口让 webview 返回", '_win["obj"].destroy()' in mp)
chk("退出钩子有 os._exit 兜底", "os._exit(0)" in mp)
chk("窗口对象已留引用(否则无法 destroy)",
    '_win["obj"] = webview.create_window' in mp)
chk("上报信息在服务就绪之后(否则 port 是 0)",
    mp.find("restart_mod.set_runtime_info(") > mp.find("if not _wait_ready(port):"))

# ===================================================================== 收尾
try:
    rs.DATA_DIR, rs.LAUNCH_PATH, rs.STATUS_PATH, rs.REQUEST_PATH, \
        rs.HELPER_PATH, rs.HELPER_LOG, _rt, rs._exit_hook = _saved
    rs._runtime.clear()
    rs._runtime.update(_rt)
except Exception:
    pass
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 72)
print("结果: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(1 if FAIL else 0)
