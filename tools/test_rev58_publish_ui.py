# -*- coding: utf-8 -*-
"""rev58 单测:发布管理「账号选项卡片」数据(各账号计数) / 三处排序 / 重启前等发布结束。

跑法:python tools/test_rev58_publish_ui.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import Storage
from backend.publish_queue import PublishQueue

PASS = FAIL = 0
FAILS = []


def ck(cond, name, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra is not None else ""))


def new_db():
    return Storage(os.path.join(tempfile.mkdtemp(prefix="rev58_"), "t.db"))


# ==================== A. 任务排序 ====================
print("\n[A] query_tasks 排序:界面倒序 / 内部旧序 / 未知值回落")

db = new_db()
F1, F2 = "finderA111", "finderB222"
t_pend1, _ = db.enqueue_task(F1, "/a/1.mp4", "1.mp4", 100, "fp1", title="t1")
t_pend2, _ = db.enqueue_task(F1, "/a/2.mp4", "2.mp4", 100, "fp2", title="t2")
t_pend3, _ = db.enqueue_task(F2, "/a/3.mp4", "3.mp4", 100, "fp3", title="t3")
t_run, _ = db.enqueue_task(F2, "/a/4.mp4", "4.mp4", 100, "fp4", title="t4")
t_ok, _ = db.enqueue_task(F1, "/a/5.mp4", "5.mp4", 100, "fp5", title="t5")
t_bad, _ = db.enqueue_task(F2, "/a/6.mp4", "6.mp4", 100, "fp6", title="t6")
assert t_pend1 < t_pend2 < t_pend3 < t_run < t_ok < t_bad, "id 应按入队顺序递增"

db.set_task_status(t_run, status="running", started=True)
db.set_task_status(t_ok, status="success", finished=True)
db.set_task_status(t_bad, status="failed", error="上传失败", finished=True)

ALL = "pending,running,success,failed"

# 默认(不传 order):内部调度依赖的旧序 —— 运行中 > 待发 > 其余,组内 id 升序
rows, total = db.query_tasks(status=ALL)
ids = [r["id"] for r in rows]
ck(total == 6, "默认序:总数 6", total)
ck(ids == [t_run, t_pend1, t_pend2, t_pend3, t_ok, t_bad],
   "默认序保持旧行为(运行中置顶 + 待发 + 其余,组内 id 升序)", ids)
ck(db.query_tasks(status="pending", limit=50)[0][0]["id"] == t_pend1,
   "★ 内部调度拿到的第一条仍是最老的 pending(_tick 依赖这个)", 
   db.query_tasks(status="pending", limit=50)[0][0]["id"])

# queue 口径:运行中置顶,其余最新在前
rows, _ = db.query_tasks(status=ALL, order="queue")
ids = [r["id"] for r in rows]
ck(ids[0] == t_run, "queue:发布中仍置顶", ids)
ck(ids[1:] == sorted(ids[1:], reverse=True), "queue:其余按 id 倒序(最新在前)", ids)
ck(ids == [t_run, t_bad, t_ok, t_pend3, t_pend2, t_pend1], "queue:完整顺序", ids)

# history 口径:按完成时间倒序
rows, _ = db.query_tasks(status="success,failed", order="history")
ids = [r["id"] for r in rows]
ck(ids == [t_bad, t_ok], "history:最新的完成记录排前面(failed 比 success 晚完成)", ids)

# 未知 / 空 order 必须回落默认序,防止前端传错值把调度顺序带跑
for bad in ("", "desc", "DROP TABLE"):
    rows, _ = db.query_tasks(status=ALL, order=bad)
    ck([r["id"] for r in rows] == [t_run, t_pend1, t_pend2, t_pend3, t_ok, t_bad],
       f"order={bad!r} 回落默认序(白名单,不拼 SQL)", [r["id"] for r in rows])

# 分页与倒序共存:queue 第 2 页应是 id 更小的那一批
p1, _ = db.query_tasks(status=ALL, order="queue", limit=2, offset=0)
p2, _ = db.query_tasks(status=ALL, order="queue", limit=2, offset=2)
ck([r["id"] for r in p1] + [r["id"] for r in p2] == [t_run, t_bad, t_ok, t_pend3],
   "queue + 分页:两页拼起来仍是倒序", ([r["id"] for r in p1], [r["id"] for r in p2]))


# ==================== B. 各账号计数(卡片上的数字) ====================
print("\n[B] task_counts_by_finder:账号卡片上的待发/已发数字")

cnt = db.task_counts_by_finder()
ck(cnt.get(F1, {}).get("pending") == 2, "F1 待发 2", cnt.get(F1))
ck(cnt.get(F1, {}).get("success") == 1, "F1 已发 1", cnt.get(F1))
ck(cnt.get(F2, {}).get("pending") == 1, "F2 待发 1", cnt.get(F2))
ck(cnt.get(F2, {}).get("running") == 1, "F2 发布中 1", cnt.get(F2))
ck(cnt.get(F2, {}).get("failed") == 1, "F2 失败 1", cnt.get(F2))
ck("finderC333" not in cnt, "没有任务的视频号不出现(前端卡片也不会多一张)")

# 卡片口径:待发 = pending + running + need_review
def pending_of(c):
    return (c.get("pending") or 0) + (c.get("running") or 0) + (c.get("need_review") or 0)

ck(pending_of(cnt[F1]) == 2, "F1 卡片数字 = 2(与「待发布」列表条数一致)")
ck(pending_of(cnt[F2]) == 2, "F2 卡片数字 = 2(1 待发 + 1 发布中)")
rows, _ = db.query_tasks(status="pending,running,need_review", finder_id=F1, order="queue")
ck(len(rows) == pending_of(cnt[F1]), "★ 卡片数字与按账号筛选后的列表条数严格相等(F1)",
   (len(rows), pending_of(cnt[F1])))
rows, _ = db.query_tasks(status="pending,running,need_review", finder_id=F2, order="queue")
ck(len(rows) == pending_of(cnt[F2]), "★ 卡片数字与按账号筛选后的列表条数严格相等(F2)",
   (len(rows), pending_of(cnt[F2])))


# ==================== C. 标题池排序 ====================
print("\n[C] query_titles:未用在前,同组最新加的在前")

db.add_titles(["旧标题1", "旧标题2", "旧标题3"], finder_id=F1)
ts = db.query_titles(finder_id=F1)
ck(len(ts) == 3, "标题 3 条", len(ts))
ck([t["content"] for t in ts] == ["旧标题3", "旧标题2", "旧标题1"],
   "最新加的排最前(倒序)", [t["content"] for t in ts])

tid, _content = db.take_title(F1)          # 用掉一条(会是最老的:take_title 仍按入池先后)
ck(tid == ts[-1]["id"], "take_title 仍取最老的(发布顺序不受显示排序影响)",
   (tid, ts[-1]["id"]))
ts2 = db.query_titles(finder_id=F1)
ck(ts2[-1]["used_count"] == 1, "已用的排到最后(未用优先,方便看还剩什么)",
   [(t["content"], t["used_count"]) for t in ts2])
ck([t["content"] for t in ts2[:2]] == ["旧标题3", "旧标题2"], "未用组内仍是倒序")


# ==================== D. 重启前判断「是否有作品在发」 ====================
print("\n[D] PublishQueue.busy_now:队列标志与库里 running 取或")


class FakeStorage:
    def __init__(self, running=0):
        self.running = running

    def query_tasks(self, status=None, limit=200, finder_id=None, order=None):
        return [], (self.running if "running" in (status or "") else 0)


q = PublishQueue(manager=None, storage=FakeStorage(0), config={})
ck(q.busy_now() is False, "都空闲 -> 不忙")

q._busy = True
ck(q.busy_now() is True, "队列标志为真 -> 忙(正在上传/填描述)")
q._busy = False

q.storage = FakeStorage(1)
ck(q.busy_now() is True, "库里还有 running -> 忙(标志已归位但任务没结束)")

q.storage = FakeStorage(0)
ck(q.busy_now() is False, "库里没有 running -> 不忙")


# ==================== E. wait_idle:等当前这一条发完 ====================
print("\n[E] wait_idle:有任务在发就等,到 max_wait 也放行")


async def t_idle_now():
    qq = PublishQueue(manager=None, storage=FakeStorage(0), config={})
    t0 = asyncio.get_event_loop().time()
    waited = await qq.wait_idle(max_wait=5, sleep=0.05)
    dt = asyncio.get_event_loop().time() - t0
    return waited, dt


waited, dt = asyncio.run(t_idle_now())
ck(0 < waited <= 0.2 and dt < 0.5, "空闲时只睡一拍就返回(不白等)", (waited, dt))


async def t_waits_until_done():
    qq = PublishQueue(manager=None, storage=FakeStorage(0), config={})
    qq._busy = True

    async def finish_later():
        await asyncio.sleep(0.3)
        qq._busy = False

    asyncio.ensure_future(finish_later())
    t0 = asyncio.get_event_loop().time()
    waited = await qq.wait_idle(max_wait=10, sleep=0.05)
    dt = asyncio.get_event_loop().time() - t0
    return waited, dt, qq.busy_now()


waited, dt, still = asyncio.run(t_waits_until_done())
ck(waited >= 0.3, "任务在 0.3s 后结束 -> 确实等到了", waited)
ck(dt < 1.0, "等到就立刻返回,不会拖满 max_wait", dt)
ck(still is False, "返回时已不忙(可以安全重启)", still)


async def t_cap():
    qq = PublishQueue(manager=None, storage=FakeStorage(1), config={})
    # 一直在发:必须到 max_wait 就放行,不能无限期等下去(否则重启按钮变成哑巴)
    waited = await qq.wait_idle(max_wait=0.4, sleep=0.1)
    return waited, qq.busy_now()


waited, still = asyncio.run(t_cap())
ck(abs(waited - 0.4) < 0.11, "max_wait 到点即放行,等待时长受控(0.4s)", waited)
ck(still is True, "仍忙由调用方打警告(强走,任务重启后转待核对)", still)


print()
print("=" * 60)
print(f"PASS {PASS}  FAIL {FAIL}")
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
