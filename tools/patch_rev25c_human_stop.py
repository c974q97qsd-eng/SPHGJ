# -*- coding: utf-8 -*-
"""rev25-3a: account_manager 增加「人工停止」标记。

用途:自动发作品调度器(rev25)在账号未运行时会自动拉起并保持运行(用户选择的行为)。
但如果用户是【手动点停】某个号,调度器不能又把它拉起来跟自己打架 ——
所以这里记一笔 human_stopped,调度器据此跳过该视频号;下次 start_account 自动清除。
"""
import io, sys

P = r"D:\SPHGJ\backend\account_manager.py"
s = io.open(P, encoding="utf-8").read()
orig = len(s)

# 1) __init__ 加字段
OLD1 = """        self._run_intent = self._load_run_intent()"""
NEW1 = """        self._run_intent = self._load_run_intent()
        # rev25: 用户手动点停的账号。自动发作品调度器不会去拉起这些号(避免跟用户操作打架),
        # 用户下次手动启动时自动清除。
        self.human_stopped = set()"""
assert s.count(OLD1) == 1, "__init__ 锚点不唯一"
s = s.replace(OLD1, NEW1, 1)

# 2) start_account 清除标记
OLD2 = """            w.start_loop()
            w.start_live_loop()
            self.workers[account_id] = w"""
NEW2 = """            w.start_loop()
            w.start_live_loop()
            self.workers[account_id] = w
            self.human_stopped.discard(account_id)  # rev25: 手动启动 -> 恢复自动发布资格"""
assert s.count(OLD2) == 1, "start_account 锚点不唯一"
s = s.replace(OLD2, NEW2, 1)

# 3) stop_account 打标记
OLD3 = """    async def stop_account(self, account_id):
        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)
        self._run_intent.discard(account_id)"""
NEW3 = """    async def stop_account(self, account_id):
        # rev21: 用户主动停 -> 清运行意图(重启后不恢复该账号)
        self._run_intent.discard(account_id)
        # rev25: 记下「人工停止」,自动发作品调度器不要替他拉起
        self.human_stopped.add(account_id)"""
assert s.count(OLD3) == 1, "stop_account 锚点不唯一"
s = s.replace(OLD3, NEW3, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
chk = io.open(P, encoding="utf-8").read()
miss = [m for m in ("self.human_stopped = set()", "self.human_stopped.discard(account_id)",
                    "self.human_stopped.add(account_id)") if m not in chk]
print(f"size {orig} -> {len(chk)}")
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
