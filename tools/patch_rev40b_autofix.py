# -*- coding: utf-8 -*-
"""rev40 接线：启动时自动执行一次「need_review 误标」数据修正。

改动 2 处（都极小）：
1. server.py 顶部加 import
2. reset_running_tasks() 收尾之后调用 fix_mislabeled_need_review()

为什么要自动跑：代码是随更新发到每台用户机的，历史脏数据必须跟着一起修，
否则每台机器都要人工敲命令 —— 用户明确要求「用户机都会更新」。
函数本身幂等 + 只回滚有硬证据的 + 写审计文件，详见 backend/publish_fixups.py。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "server.py")


def main():
    s = io.open(P, encoding="utf-8").read()

    if "fix_mislabeled_need_review" in s:
        print("rev40 接线已应用过，跳过。")
        return 0

    # ---- 1. import ----
    old_imp = "from .publish_queue import PublishQueue\n"
    new_imp = ("from .publish_queue import PublishQueue\n"
               "from .publish_fixups import fix_mislabeled_need_review   # rev40: 历史误标回滚\n")
    assert s.count(old_imp) == 1, "import 锚点命中 %d 次" % s.count(old_imp)
    s = s.replace(old_imp, new_imp, 1)

    # ---- 2. 调用点（紧跟 running 收尾之后）----
    old_call = '''except Exception as _e:
    logger.warning(f"[publish] 收尾 running 任务失败(不影响启动): {_e}")
'''
    new_call = '''except Exception as _e:
    logger.warning(f"[publish] 收尾 running 任务失败(不影响启动): {_e}")

# rev40: 旧版 reset_running_tasks() 的 UPDATE 漏了 WHERE —— 只要启动时存在 1 条
# running，全表(含已 success / failed)都会被刷成 need_review。代码已修，
# 历史脏数据在这里一次性回滚：只回滚有「已发布」硬证据的，其余保留人工核对。
# 幂等、写审计(data/rev40_fix_audit.json)、异常不影响启动。
try:
    _fx = fix_mislabeled_need_review(storage.db_path, publish_queue.inbox_dir, logger)
    if _fx.get("recover") and not _fx.get("skipped"):
        logger.warning(f"[publish] rev40 历史误标修复: {_fx['recover']} 条 need_review 已确认为"
                       f"「实际已发布」并转回 success（仍保留待核对 {_fx.get('kept', 0)} 条）")
except Exception as _e:
    logger.warning(f"[publish] rev40 历史误标修复跳过(不影响启动): {_e}")
'''
    assert s.count(old_call) == 1, "调用点锚点命中 %d 次" % s.count(old_call)
    s = s.replace(old_call, new_call, 1)

    io.open(P, "w", encoding="utf-8", newline="").write(s)

    t = io.open(P, encoding="utf-8").read()
    checks = [
        ("import 已加", "from .publish_fixups import fix_mislabeled_need_review" in t),
        ("调用点在 reset 之后",
         t.find("fix_mislabeled_need_review(storage.db_path") >
         t.find("_nr = storage.reset_running_tasks()")),
        ("包在 try 里", "rev40 历史误标修复跳过" in t),
    ]
    ok = True
    for name, cond in checks:
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    import py_compile
    try:
        py_compile.compile(P, doraise=True)
        print("  PASS 语法检查")
    except Exception as e:
        print("  FAIL 语法检查:", e)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
