# -*- coding: utf-8 -*-
"""rev63b: 让 circuit_cooldown_sec=0 真正退回「永久熔断,只能手动」的旧行为。

说明文档给的代码 `if cool > 0 and waited < cool:` 在 cool=0 时会直接落到
最后的 `return True`,等于「立即半开」,与文档自己写的「0 = 永久熔断」相反。
这里补一个 cool<=0 的显式分支。
"""
import io
import os
import sys

PQ = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "backend", "publish_queue.py")

OLD = '''                cool = float(self._cfg_get("circuit_cooldown_sec") or 0)
                last_fail = float(self._fail_at.get(fid) or 0)
                waited = (now - last_fail) if last_fail else 1e9
                if cool > 0 and waited < cool:
'''

NEW = '''                cool = float(self._cfg_get("circuit_cooldown_sec") or 0)
                if cool <= 0:
                    # 配置 0 = 保留旧行为:永久熔断,只能手动解除或重启进程
                    return False, f"连续失败 {streak} 次已自动暂停该视频号,请在发布记录里核对后手动重试"
                last_fail = float(self._fail_at.get(fid) or 0)
                # 没有时间戳(升级前就已熔断/手工塞的值)一律视为冷却已过,
                # 否则会重现「永远不归零」的死锁
                waited = (now - last_fail) if last_fail else 1e9
                if waited < cool:
'''

s = io.open(PQ, encoding="utf-8").read()
assert s.count(OLD) == 1, "锚点命中 %d 次" % s.count(OLD)
s = s.replace(OLD, NEW, 1)
io.open(PQ, "w", encoding="utf-8", newline="").write(s)

s = io.open(PQ, encoding="utf-8").read()
checks = [
    ("cool<=0 分支", "if cool <= 0:" in s),
    ("cool<=0 用旧文案", "已自动暂停该视频号,请在发布记录里核对后手动重试" in s),
    ("半开分支保留", "logger.info(f\"[publish] {self.finder_label(fid)} 熔断冷却期已过,半开试发一条\")" in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print("  %-20s %s" % (n, "OK" if ok else "FAIL"))
if bad:
    print("SELFCHECK FAIL", bad)
    sys.exit(1)
print("SELFCHECK PASS")
