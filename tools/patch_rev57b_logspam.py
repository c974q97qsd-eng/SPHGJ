# -*- coding: utf-8 -*-
"""rev57b 补丁:给「重放场次 ID 已刷新」日志去重。

问题:母本 data/live_api_spec.json 里模板仍带旧场次 ID,而 _fresh_post_data 每轮
重放都会比对一次 —— old 恒为那个旧 ID、oid 恒为本场 ID,于是【每轮每账号打一条
INFO】。4 个在播账号 × 约 7.5 轮/分钟 ≈ 1800 条/小时,把 run.log 淹掉。

修法:同一次会话里对同一个 (旧ID -> 新ID) 组合只打一次;换场后 oid 变了,
组合跟着变,会再打一条 —— 既能看出"修复生效",又不刷屏。
不改母本运行时数据,对所有用户机一起生效。
"""
import io
import sys

P = r"D:\SPHGJ\backend\live_fetcher.py"
s = io.open(P, encoding="utf-8").read()
orig = s

OLD_INIT = """        self.live_start_ts = None       # check_live_status.createTime(开播时刻,Unix 秒)
        self._empty_info_warn_ts = 0.0  # get_live_info 返回空 data 的告警限流(秒)
"""
NEW_INIT = """        self.live_start_ts = None       # check_live_status.createTime(开播时刻,Unix 秒)
        self._empty_info_warn_ts = 0.0  # get_live_info 返回空 data 的告警限流(秒)
        # rev57b: 「场次 ID 已刷新」日志的幂等标记。缓存模板里的旧 ID 是固定的,
        # 不去重就会每轮重放都打一条 INFO(4 账号约 1800 条/小时),把 run.log 淹掉。
        self._oid_logged = None         # 上次已打日志的 (旧ID, 新ID) 组合
"""

OLD_LOG = """        old = obj.get("liveObjectId") or ""
        if old and str(old) != str(oid):
            logger.info(f"[live:{self.account_id}] 重放场次 ID 已刷新: {old} -> {oid}")
"""
NEW_LOG = """        old = obj.get("liveObjectId") or ""
        # rev57b: 同一 (旧, 新) 组合只打一次 —— 否则模板里那个固定的旧 ID 会让每轮
        # 重放都打一条 INFO。换场后 oid 变了,组合跟着变,会重新打一条。
        if old and str(old) != str(oid):
            _pair = (str(old), str(oid))
            if self._oid_logged != _pair:
                self._oid_logged = _pair
                logger.info(f"[live:{self.account_id}] 重放场次 ID 已刷新: {old} -> {oid}")
"""

for tag, old, new in [("__init__ 幂等标记", OLD_INIT, NEW_INIT),
                      ("刷新日志去重", OLD_LOG, NEW_LOG)]:
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次(应为 1),中止")
        sys.exit(1)
    s = s.replace(old, new, 1)
    print(f"[ok] {tag}")

io.open(P, "w", encoding="utf-8", newline="").write(s)
print(f"\n已写回 {P} ({len(orig)} -> {len(s)} 字节)")

s2 = io.open(P, encoding="utf-8").read()
CHECKS = [
    ("_oid_logged 初始化", "self._oid_logged = None" in s2),
    ("日志去重分支", "if self._oid_logged != _pair:" in s2),
    ("仍会打日志", "重放场次 ID 已刷新" in s2),
]
bad = 0
print()
for name, ok in CHECKS:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad += 0 if ok else 1
print("\nSELFCHECK", "PASS" if bad == 0 else f"FAIL({bad})")
sys.exit(1 if bad else 0)
