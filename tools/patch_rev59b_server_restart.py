# -*- coding: utf-8 -*-
"""rev59b 补丁:server.py 重启接口透传 restart() 的 already 语义。

restart.py 现在会在 120s 内拒绝第二次安排(返回 already=True),
接口必须把这件事如实告诉调用方 —— 否则前端会以为"正在重启"然后干等
一个根本不会发生的 pid 变化。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "server.py")

PATCHES = [
    # wait_task 后的延迟重启:重复安排只记日志,不再循环重试
    ('''                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                restart_mod.restart(delay)''',
     '''                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                info = restart_mod.restart(delay)
                if info.get("already"):
                    logger.info(f"[restart] 已经安排过重启(helper_pid="
                                f"{info.get('helper_pid')}),本次不重复拉起")''',
     "延迟重启透传 already"),

    # 正常路径:already 时返回专门的状态,前端不会白等
    ('''    logger.info(f"[restart] 收到远程重启请求,{delay}s 后退出 "
                f"(helper_pid={info['helper_pid']}, wait_task={int(bool(wait_task))})")
    return {"ok": True, "state": "restarting", **info,
            "hint": "本进程将在 delay 秒后退出,请轮询 /api/system/info 确认新 pid"}''',
     '''    if info.get("already"):
        # rev59: 同一进程 120s 内只允许安排一次重启(否则会拉起多个 helper,
        # 极端情况出现两个新实例抢同一批 profile)
        return {"ok": True, "state": "already", **info,
                "hint": f"{info.get('since', 0):.0f} 秒前已经安排过一次重启,"
                        f"本次不再重复拉起;继续等 pid 变化即可"}
    logger.info(f"[restart] 收到远程重启请求,{delay}s 后退出 "
                f"(helper_pid={info['helper_pid']}, wait_task={int(bool(wait_task))})")
    return {"ok": True, "state": "restarting", **info,
            "hint": "本进程将在 delay 秒后退出,请轮询 /api/system/info 确认新 pid"}''',
     "接口返回 state=already"),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    n = s.count(old)
    assert n == 1, f"{tag}: 命中 {n} 次(期望 1)"
    s = s.replace(old, new, 1)
    print(f"ok  {tag}")
io.open(P, "w", encoding="utf-8", newline="").write(s)

s2 = io.open(P, encoding="utf-8").read()
checks = [
    ("state=already 分支", '"state": "already"' in s2),
    ("延迟重启透传", 'if info.get("already"):\n                    logger.info' in s2),
    ("原 restarting 分支保留", '"state": "restarting"' in s2),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS " if ok else "  FAIL ") + n)
print("\nSELFCHECK", "PASS" if not bad else f"FAIL {bad}")
