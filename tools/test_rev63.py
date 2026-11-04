# -*- coding: utf-8 -*-
"""rev63 单测：发布熔断不再误判掉线 + 熔断冷却半开。

全部用 stub，不连浏览器、不写生产库。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend import publish_queue as PQ  # noqa: E402
from backend.publish_queue import PublishQueue, _OFFLINE_TAG  # noqa: E402

FID = "FID_TEST_0001"
AID = "accX"

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "FAIL", name, ("  <- " + str(extra)) if (extra and not cond) else ""))


# ------------------------------------------------------------------ stubs
class StStorage:
    def __init__(self):
        self.calls = []
        self.task = {
            "id": 1, "finder_id": FID, "title": "测试标题",
            "src_path": os.path.abspath(__file__), "attempts": 0, "title_id": None,
            "file_name": "t.mp4",
        }

    def get_task(self, tid):
        return dict(self.task)

    def set_task_status(self, tid, **kw):
        self.calls.append(kw)

    def daily_success_count(self, fid, day):
        return 0

    def last_success_at(self, fid):
        return None

    def release_title(self, tid):
        pass


class StManager:
    def __init__(self, start_returns=None, known_offline=False):
        self.workers = {}
        self.human_stopped = set()
        self._start_returns = start_returns
        self._known = known_offline
        self.start_calls = 0

    def _is_known_offline(self, acc):
        return self._known

    async def start_account(self, aid):
        self.start_calls += 1
        return self._start_returns


def mkq(manager=None, storage=None, publish_cfg=None, accounts=None):
    cfg = {
        "publish": publish_cfg or {},
        "accounts": accounts if accounts is not None else [
            {"id": AID, "_log_finder_id": FID, "name": "测试店"},
        ],
    }
    return PublishQueue(manager or StManager(), storage or StStorage(), cfg)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------------ 1 常量
print("\n== 1. 常量与结构 ==")
check("_OFFLINE_TAG 存在且非空", isinstance(_OFFLINE_TAG, str) and _OFFLINE_TAG)
check("DEFAULTS 有 circuit_cooldown_sec=1800",
      PQ.DEFAULTS.get("circuit_cooldown_sec") == 1800, PQ.DEFAULTS.get("circuit_cooldown_sec"))

q = mkq()
check("_fail_at 已初始化", q._fail_at == {})

# ------------------------------------------------------------------ 2 _bump
print("\n== 2. _bump_fail_streak 计数+时间戳 ==")
t0 = time.time()
q._bump_fail_streak(FID)
check("count=1", q._fail_streak.get(FID) == 1, q._fail_streak)
check("时间戳已打", (q._fail_at.get(FID) or 0) >= t0 - 1, q._fail_at)
q._bump_fail_streak(FID)
check("count=2", q._fail_streak.get(FID) == 2)

# ------------------------------------------------------------------ 3 冷却期
print("\n== 3. can_run 熔断冷却期 ==")
task = {"finder_id": FID}
q3 = mkq(publish_cfg={"window_start": "00:00", "window_end": "23:59",
                      "circuit_cooldown_sec": 1800})
q3._fail_streak[FID] = 2
q3._fail_at[FID] = time.time() - 60          # 刚失败 1 分钟
ok, why = q3.can_run(task)
check("冷却期内拒绝", ok is False, (ok, why))
check("文案含「熔断」和「自动试发」", "熔断" in why and "自动试发" in why, why)
check("文案给出剩余分钟", "分钟" in why, why)

# 冷却期满 -> 半开
q3._fail_at[FID] = time.time() - 1801
ok2, why2 = q3.can_run(task)
check("冷却期满放行(半开)", ok2 is True, (ok2, why2))
check("streak 仍为 2(等成功才清零)", q3._fail_streak.get(FID) == 2)

# 边界:刚好差 1 秒
q3._fail_at[FID] = time.time() - 1799
ok3, why3 = q3.can_run(task)
check("1799s 仍拒", ok3 is False, (ok3, why3))

# 半开后又失败 -> 刷新时间戳,重新进冷却
q3._bump_fail_streak(FID)
ok4, why4 = q3.can_run(task)
check("半开再失败重新冷却", ok4 is False and q3._fail_streak[FID] == 3, (ok4, why4, q3._fail_streak[FID]))

# ------------------------------------------------------------------ 4 cooldown=0 旧行为
print("\n== 4. circuit_cooldown_sec=0 退回永久熔断 ==")
q4 = mkq(publish_cfg={"window_start": "00:00", "window_end": "23:59",
                      "circuit_cooldown_sec": 0})
q4._fail_streak[FID] = 2
q4._fail_at[FID] = time.time() - 999999
ok5, why5 = q4.can_run(task)
check("永久熔断", ok5 is False, (ok5, why5))
check("文案不提自动试发", "自动试发" not in why5, why5)

# ------------------------------------------------------------------ 5 无时间戳(历史熔断)
print("\n== 5. 老熔断没有 _fail_at(进程内升级/手动塞值) ==")
q5 = mkq(publish_cfg={"window_start": "00:00", "window_end": "23:59",
                      "circuit_cooldown_sec": 1800})
q5._fail_streak[FID] = 2
ok6, why6 = q5.can_run(task)
check("无时间戳直接放行,避免死锁", ok6 is True, (ok6, why6))

# ------------------------------------------------------------------ 6 _ensure_worker
print("\n== 6. _ensure_worker 区分掉线与真失败 ==")
m_off = StManager(start_returns=None, known_offline=True)
q6 = mkq(manager=m_off)
w, err = run(q6._ensure_worker(FID))
check("已知掉线 -> err 带 _OFFLINE_TAG", err.startswith(_OFFLINE_TAG), err)
check("已知掉线 -> 文案说明不计入熔断", "不计入熔断" in err, err)

m_bad = StManager(start_returns=None, known_offline=False)
q6b = mkq(manager=m_bad)
w, err2 = run(q6b._ensure_worker(FID))
check("浏览器起不来 -> err 不带 tag", not err2.startswith(_OFFLINE_TAG), err2)
check("浏览器起不来 -> 是失败文案", "失败" in err2, err2)


class W:
    logged_in = False


m_nl = StManager(start_returns=W(), known_offline=False)
q6c = mkq(manager=m_nl)
w, err3 = run(q6c._ensure_worker(FID))
check("w.logged_in=False -> err 带 tag", err3.startswith(_OFFLINE_TAG), err3)


# ------------------------------------------------------------------ 7 run_task 计数
print("\n== 7. run_task 只有真失败才 +streak ==")
m1 = StManager(start_returns=None, known_offline=True)
s1 = StStorage()
q7 = mkq(manager=m1, storage=s1)
res = run(q7.run_task(1))
check("掉线:streak 保持 0", q7._fail_streak.get(FID, 0) == 0, q7._fail_streak)
check("掉线:任务留 pending", s1.calls and s1.calls[-1].get("status") == "pending", s1.calls[-1:])
check("掉线:finished=False", s1.calls and s1.calls[-1].get("finished") is False, s1.calls[-1:])
check("掉线:错误里带 tag", str(s1.calls[-1].get("error") or "").startswith(_OFFLINE_TAG), s1.calls[-1:])

m2 = StManager(start_returns=None, known_offline=False)
s2 = StStorage()
q8 = mkq(manager=m2, storage=s2)
run(q8.run_task(1))
check("真失败:streak=1", q8._fail_streak.get(FID, 0) == 1, q8._fail_streak)
check("真失败:打了时间戳", (q8._fail_at.get(FID) or 0) > 0, q8._fail_at)
check("真失败:任务 failed", s2.calls and s2.calls[-1].get("status") == "failed", s2.calls[-1:])

# 连跑两次真失败 -> 熔断
run(q8.run_task(1))
check("两次真失败 streak=2", q8._fail_streak.get(FID) == 2)
q8._fail_at[FID] = time.time()
okc, whyc = q8.can_run(task)
check("两次真失败后 can_run 拒绝", okc is False and "熔断" in whyc, (okc, whyc))

# ------------------------------------------------------------------ 8 源码级断言
print("\n== 8. 源码级断言 ==")
src = open(r"D:\SPHGJ\backend\publish_queue.py", encoding="utf-8").read()
check("成功分支清 _fail_at", "self._fail_streak[fid] = 0\n                self._fail_at.pop(fid, None)" in src)
check("真失败分支走 _bump", "self._bump_fail_streak(fid)\n                self.storage.set_task_status(" in src)
check("旧的一律计数已删", "self._fail_streak[fid] = self._fail_streak.get(fid, 0) + 1\n                return" not in src)

sv = open(r"D:\SPHGJ\backend\server.py", encoding="utf-8").read()
check("server 有 reset-streak 路由", '/api/publish/finders/{finder_id}/reset-streak' in sv)
check("run/retry 各清一次 _fail_at", sv.count("publish_queue._fail_at.pop(_fid, None)") == 2)

apits = open(r"D:\SPHGJ\frontend\src\lib\api.ts", encoding="utf-8").read()
spts = open(r"D:\SPHGJ\frontend\src\components\publish\SettingsPanel.tsx", encoding="utf-8").read()
check("前端 api 有 publishResetStreak", "publishResetStreak" in apits)
check("SettingsPanel 有「解除」按钮", ">解除</Button>" in spts)

# ------------------------------------------------------------------ 汇总
print("\n==== 通过 %d / 失败 %d ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL PASS")
