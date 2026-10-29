# -*- coding: utf-8 -*-
"""rev59 补丁:restart.py —— 并发重启保护 + helper 不重复拉起新实例。

两个真实风险(都和"2 分钟连点 3 次重启"有关):
  1) 每次点都会起一个独立 helper,各自"等旧进程死 -> 拉新实例"。
     一旦旧进程死得比 helper 判断快,就可能同时拉起两个新实例:
     一个占 8712、一个退到 8713,两边同时抢同一批 profile 锁 —— 比重启失败更难查。
  2) 用户手动双击启动时也有同样问题,helper 需要先确认"外面已经有一个新实例了"。

改法:
  - restart() 里记 _inflight,120s 内重复请求直接返回 {"already": True}(不起 helper)。
  - helper 拉新实例之前先探 /api/system/info:发现 pid != old_pid 就认领它,不再拉起。
  - helper 等旧进程退出的上限从 45s 提到 75s —— rev59 起旧进程会等浏览器真正关完
    (最多 8s 排空连接 + 20s 关浏览器),45s 有点紧,紧到会误杀正在收尾的进程。

用法: python tools/patch_rev59_restart.py
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "restart.py")

PATCHES = []

# ---------------- 1. 并发重启保护 ----------------
PATCHES.append((
    '''def restart(delay=3.0, ports=None):
    """安排一次重启:先起 helper,再让本进程优雅退出。"""
    delay = max(1.0, min(float(delay), 60.0))
    helper_pid = spawn_helper(delay, ports=ports)
''',
    '''# rev59: 同一个进程只允许安排一次重启。
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
''',
    "restart() 并发保护",
))

# ---------------- 2. helper:先确认没有别的新实例 ----------------
PATCHES.append((
    '''def wait_api(ports, timeout, old_pid):''',
    '''def running_pid(ports):
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


def wait_api(ports, timeout, old_pid):''',
    "helper: 新增 running_pid()",
))

PATCHES.append((
    '''    # 1) 等旧进程自然退出(主程序会优雅关闭浏览器,可能要几秒)
    deadline = time.time() + 45''',
    '''    # 1) 等旧进程自然退出(主程序会优雅关闭浏览器,可能要十几秒)
    # rev59: 旧进程现在会「排空连接(<=8s)+ 关浏览器(<=20s)」才退出,45s 太紧,
    # 紧到会误杀正在收尾的进程 —— 那就又变成硬砍 Chromium 了。
    deadline = time.time() + 75''',
    "helper: 等旧进程上限 45 -> 75s",
))

PATCHES.append((
    '''    # 2) 等端口释放
    deadline = time.time() + 20
    while time.time() < deadline and any(port_open(p) for p in ports):
        time.sleep(0.5)
''',
    '''    # 1.5) 已经有别的新实例在跑?认领它,别再造一个(rev59)
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
''',
    "helper: 拉实例前先查已有实例",
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
# 内嵌 helper 源码必须能被独立编译(语法错误只有到运行时才会炸)
helper_src = s2.split("HELPER_SOURCE = r'''", 1)[1].rsplit("'''", 1)[0]
compile(helper_src, "HELPER_SOURCE", "exec")

checks = [
    ("_inflight 定义", "_inflight = {\"at\": 0.0, \"helper_pid\": 0}" in s2),
    ("并发保护分支", 'if _inflight["at"] and since < INFLIGHT_WINDOW:' in s2),
    ("helper 有 running_pid", "def running_pid(ports):" in helper_src),
    ("helper 跳过重复拉起", "跳过拉起" in helper_src),
    ("helper 等 75s", "deadline = time.time() + 75" in helper_src),
    ("helper 仍先等旧进程", helper_src.index("deadline = time.time() + 75")
                            < helper_src.index("port0, pid0 = running_pid(ports)")),
    ("helper 仍会等端口释放", "while time.time() < deadline and any(port_open(p) for p in ports)" in helper_src),
    ("HELPER_SOURCE 可编译", True),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("\nSELFCHECK", "PASS" if not bad else f"FAIL {bad}")
