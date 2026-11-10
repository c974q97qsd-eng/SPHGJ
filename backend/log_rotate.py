# -*- coding: utf-8 -*-
"""rev69: 运行日志轮转 —— 复制归档 + 启动时用 "w" 打开(截断)。

## 为什么不用 rename,也不用 truncate

母本实测了两轮,两条路都被堵死:

1. `os.replace(run.log, logs/run-xxx.log)` —— `PermissionError [WinError 32]
   另一个程序正在使用此文件`。重启助手进程继承了上一代的 stdout 句柄,
   helper 活着期间这个文件就有别的写者,Python 的 open() 不带 FILE_SHARE_DELETE,
   改名必失败。

2. 「复制 + 对本进程句柄 truncate」—— 实测 `/api/system/log` 回报
   `has_handle: false`:server 进程里 sys.stdout / sys.stderr **都不是** run.log
   的句柄,所以根本没有可以 truncate 的对象。

## 现在怎么做

**完全不动 run.log 的名字,也不对它做 truncate**,只做两件绝对安全的事:

    1. 用独立读句柄把 run.log 整份**复制**到 logs/run-<mtime>.log
       (纯读,任何写者都不会被影响)
    2. 告诉调用方(main.py)用 **"w" 模式**打开 run.log

第 2 步是关键:启动前先决定 mode,需要归档就是 "w"(打开即截断),
不需要就是 "a"(照常追加)。`open(..., "w")` 在有别的写者时也允许
(截断属于写操作,不涉及 delete 权限),所以不会 WinError 32。

"要不要归档"和"怎么打开文件"由 plan_startup 一次性决定、main.py 只照抄 mode,
不会出现"判定要归档但打开时又追加到旧文件"的错位。

## 运行中超限

运行中不碰文件(没有可靠句柄),只检查大小;超过阈值由调用方安排一次优雅重启
(约 12s,账号自动恢复),重启时 plan_startup 自然会归档 + 截断。

## 命名与保留

    run.log  ->  logs/run-YYYYmmdd-HHMMSS.log   (时间取原文件 mtime)
    logs/ 下超过 keep_days 天的归档删除

启动条件:文件非空 且 (超过 max_mb 或 不是今天修改的) —— 后者保证每天至少一份。
"""
import logging
import os
import shutil
import time

logger = logging.getLogger("sphgj")

# 最近一次失败 / 成功的原因(给 /api/system/log 看)。
# 前两版就是因为异常被静默吞掉,母本上完全看不出为什么没归档。
LAST_ERROR = ""
LAST_ARCHIVE = ""

DEFAULT_MAX_MB = 20
DEFAULT_KEEP_DAYS = 7
DEFAULT_CHECK_SEC = 6 * 3600
ARCHIVE_PREFIX = "run-"
ARCHIVE_SUFFIX = ".log"


def log_path(base):
    return os.path.join(base, "run.log")


def logs_dir(base):
    d = os.path.join(base, "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def handle_of(base):
    """本进程若持有 run.log 的写句柄就返回它,否则 None(仅用于诊断上报)。"""
    import sys as _sys
    want = os.path.abspath(log_path(base))
    for f in (getattr(_sys, "stdout", None), getattr(_sys, "stderr", None)):
        try:
            name = getattr(f, "name", None)
            if name and os.path.abspath(name) == want:
                return f
        except Exception:
            pass
    return None


def size_mb(base):
    """当前 run.log 大小(MB);不存在返回 0。"""
    try:
        return os.path.getsize(log_path(base)) / 1048576.0
    except Exception:
        return 0.0


def archive_path(base, src, size=None, reuse=True):
    """给 src 生成归档路径。返回 (path, already_exists)。

    已有"同名且同大小"的归档时直接复用 —— 手动归档后紧接着重启会走两次判定,
    没有这个检查就会在 logs/ 里留两份一模一样的文件。
    """
    try:
        mt = os.path.getmtime(src)
    except Exception:
        mt = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(mt))
    d = logs_dir(base)
    p = os.path.join(d, "%s%s%s" % (ARCHIVE_PREFIX, ts, ARCHIVE_SUFFIX))
    if reuse and size is not None:
        try:
            if os.path.exists(p) and os.path.getsize(p) == size:
                return p, True
        except Exception:
            pass
    i = 1
    while os.path.exists(p):
        p = os.path.join(d, "%s%s-%d%s" % (ARCHIVE_PREFIX, ts, i, ARCHIVE_SUFFIX))
        i += 1
    return p, False


def cleanup(base, keep_days=DEFAULT_KEEP_DAYS):
    """删除 logs/ 里超过 keep_days 天的归档。返回删除个数。"""
    if keep_days is None or keep_days <= 0:
        return 0
    d = logs_dir(base)
    cutoff = time.time() - keep_days * 86400
    n = 0
    try:
        for f in os.listdir(d):
            if not (f.startswith(ARCHIVE_PREFIX) and f.endswith(ARCHIVE_SUFFIX)):
                continue
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def _need_archive(base, max_mb):
    """返回 (需要归档?, 原因, 大小字节)。不需要时原因为 ""。"""
    p = log_path(base)
    try:
        if not os.path.exists(p):
            return False, "", 0
        sz = os.path.getsize(p)
        if sz <= 0:
            return False, "", 0
        mt = os.path.getmtime(p)
        cross_day = (time.strftime("%Y-%m-%d", time.localtime(mt))
                     != time.strftime("%Y-%m-%d", time.localtime()))
        over = sz >= float(max_mb) * 1048576
        if not cross_day and not over:
            return False, "", sz
        return True, ("超过 %gMB" % float(max_mb) if over else "跨天"), sz
    except Exception as e:
        globals()["LAST_ERROR"] = "%s: %s" % (type(e).__name__, e)
        return False, "", 0


def copy_archive(base, reason="", keep_days=DEFAULT_KEEP_DAYS, sz=None):
    """把 run.log 整份复制到 logs/。**只读源文件,绝不改它**。返回 info 或 None。"""
    global LAST_ERROR, LAST_ARCHIVE
    p = log_path(base)
    try:
        if sz is None:
            sz = os.path.getsize(p)
        dst, existed = archive_path(base, p, size=sz)
        if not existed:
            shutil.copyfile(p, dst)
        removed = cleanup(base, keep_days)
        info = {"archived_from_mb": round(sz / 1048576.0, 2),
                "archived_to": os.path.basename(dst),
                "reason": reason or "",
                "dedup": existed,
                "removed": removed}
        LAST_ARCHIVE = "%sMB -> %s(%s)%s" % (
            info["archived_from_mb"], info["archived_to"], reason,
            ",复用已有归档" if existed else "")
        LAST_ERROR = ""
        return info
    except Exception as e:
        LAST_ERROR = "%s: %s" % (type(e).__name__, e)
        return None


def plan_startup(base, max_mb=DEFAULT_MAX_MB, keep_days=DEFAULT_KEEP_DAYS):
    """启动前调用,决定 main.py 该怎么打开 run.log。

    返回 (info_or_None, open_mode):
        "w"  需要归档 -> 调用方用 w 打开(打开即截断旧内容)
        "a"  不需要归档 -> 照常追加

    复制失败时**不降级成 "w"** —— 那会把还没归档的日志直接丢掉。
    **绝不抛异常** —— 日志轮转失败不能影响程序启动。
    """
    try:
        need, reason, sz = _need_archive(base, max_mb)
        if not need:
            removed = cleanup(base, keep_days)
            return ({"removed": removed} if removed else None), "a"
        info = copy_archive(base, reason=reason, keep_days=keep_days, sz=sz)
        if info is None:
            return None, "a"          # 复制失败:保持追加,绝不截断
        return info, "w"
    except Exception as e:
        globals()["LAST_ERROR"] = "%s: %s" % (type(e).__name__, e)
        return None, "a"


def archive_now(base, keep_days=DEFAULT_KEEP_DAYS):
    """手动归档(只复制,不动 run.log)。调用方随后应安排重启把旧内容截掉。"""
    need, reason, sz = _need_archive(base, 0)     # max_mb=0 -> 有内容就归档
    if not need:
        return None
    return copy_archive(base, reason=reason or "手动", keep_days=keep_days, sz=sz)


def should_rotate_running(base, max_mb=DEFAULT_MAX_MB):
    """运行中检查:是否需要轮转(True 时由调用方触发优雅重启)。"""
    try:
        return size_mb(base) >= float(max_mb)
    except Exception:
        return False
