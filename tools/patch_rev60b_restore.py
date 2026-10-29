# -*- coding: utf-8 -*-
"""rev60b:补两个收尾判断。

问题:stop_all 会去 w.stop() 掉「启动中」的 worker,但 worker.start() 的剩余流程
还会继续跑 —— self._running=True、start_live_detect() 又起一个新 task。虽然进程
马上要退,但停机期间还起后台任务不干净(uvicorn 收尾时统一取消,日志里那些
「仍有 N 个后台任务」就是这类遗留)。

改法:
  1) start_account 在 w.start() 返回后检查 _shutting_down,是则收拾掉 worker、
     直接返回 None(不进 workers、不动 intent);
  2) restore_running_accounts 在 start_account 返回后再查一次,停机就直接 return,
     免得打一条措辞不准的「本次没启动成功」。
"""
import io

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = [
    (
        """        finally:
            self._starting.pop(account_id, None)
        if w.logged_in:""",
        """        finally:
            self._starting.pop(account_id, None)
        # rev60: 启动过程中撞上停机 —— 浏览器已被 stop_all 关掉,w.start() 剩下的
        # 流程却会继续跑(_running=True、start_live_detect 再起一个 task)。
        # 这里直接收干净并当"没启动",运行意图原样保留。
        if self._shutting_down:
            logger.info(f"[{account_id}] 启动过程中进入停机,放弃本次启动(运行意图保留)")
            try:
                await w.stop()
            except Exception as e:
                logger.debug(f"[{account_id}] 放弃启动时清理异常(忽略): {e}")
            return None
        if w.logged_in:""",
        "1 start_account 停机放弃",
    ),
    (
        """            try:
                w = await self.start_account(aid)
                if aid in self.workers:
                    continue""",
        """            try:
                w = await self.start_account(aid)
                if aid in self.workers:
                    continue
                if self._shutting_down:
                    # 启动途中开始停机:不算失败,不算未登录,直接收手
                    logger.warning(f"[恢复] 正在停机,中止自动恢复(已跳过 {len(todo) - i} 个,"
                                   f"运行意图保留,下次启动继续)")
                    return""",
        "2 restore 失败判定前再查停机",
    ),
]


def apply():
    s = io.open(P, encoding="utf-8").read()
    for old, new, tag in PATCHES:
        n = s.count(old)
        assert n == 1, f"{tag}: 命中 {n} 次"
        s = s.replace(old, new, 1)
        print(f"  ok  {tag}")
    io.open(P, "w", encoding="utf-8", newline="").write(s)


def selfcheck():
    a = io.open(P, encoding="utf-8").read()
    checks = [
        ("start_account 停机放弃", "启动过程中进入停机,放弃本次启动(运行意图保留)" in a),
        ("restore 二次检查", a.count("正在停机,中止自动恢复") == 3),
    ]
    for n, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print("\nSELFCHECK " + ("PASS" if all(ok for _, ok in checks) else "FAIL"))
    return all(ok for _, ok in checks)


if __name__ == "__main__":
    import sys
    print("=== 应用 rev60b ===")
    apply()
    print("\n=== 自校验 ===")
    sys.exit(0 if selfcheck() else 1)
