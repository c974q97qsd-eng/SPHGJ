# -*- coding: utf-8 -*-
"""rev40 回归：reset_running_tasks 只能动 running，绝不能碰历史记录。

旧版 UPDATE 漏 WHERE —— 只要启动时存在 1 条 running，全表（含已 success / failed）
统统刷成 need_review。现场后果：306 条待核对里 259 条其实已发布成功，
自动发布被当成登录/状态异常。

本测试专盯这一点，不依赖任何真实数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.storage import Storage  # noqa: E402

PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, extra)


def main():
    tmp = tempfile.mkdtemp(prefix="rev40_")
    db = os.path.join(tmp, "t.db")
    st = Storage(db)
    F = "finderA"

    # 造 6 条：1 running + 1 success + 1 failed + 2 历史 need_review + 1 pending
    ids = {}
    for i, tag in enumerate(["run", "ok", "bad", "nr1", "nr2", "pend"], 1):
        tid, created = st.enqueue_task(F, r"D:\inbox\%s.mp4" % tag, "%s.mp4" % tag,
                                       1000 + i, "fp-%s" % tag, "标题%s" % tag, 0)
        assert created, "入队失败 %s" % tag
        ids[tag] = tid

    st.set_task_status(ids["run"], status="running", step="upload", started=True)
    st.set_task_status(ids["ok"], status="success", step="done", finished=True,
                       object_id="obj-1")
    st.set_task_status(ids["bad"], status="failed", step="upload",
                       error="网络超时")
    st.set_task_status(ids["nr1"], status="need_review", step="done",
                       error="人工已核对:实际已发出")
    st.set_task_status(ids["pend"], status="pending")

    before = {t: st.get_task(i) for t, i in ids.items()}

    print("\n[1] 正常收尾：只动 running")
    n = st.reset_running_tasks()
    ck(n == 1, "返回条数 == 1（等于 running 数，不是全表数）", "got %r" % n)
    after = {t: st.get_task(i) for t, i in ids.items()}
    ck(after["run"]["status"] == "need_review", "running -> need_review")
    ck("进程中断" in (after["run"]["last_error"] or ""), "running 的 last_error 被填上提示",
       repr(after["run"]["last_error"]))
    ck(after["ok"]["status"] == "success", "★ 已 success 的不被改成 need_review",
       repr(after["ok"]["status"]))
    ck(after["bad"]["status"] == "failed", "★ 已 failed 的不被改成 need_review",
       repr(after["bad"]["status"]))
    ck(after["pend"]["status"] == "pending", "★ pending 的不被改成 need_review",
       repr(after["pend"]["status"]))
    ck(after["nr1"]["status"] == "need_review", "原本 need_review 保持 need_review")
    ck(after["nr1"]["last_error"] == before["nr1"]["last_error"],
       "★ 原本 need_review 的 last_error 不被覆盖", repr(after["nr1"]["last_error"]))
    ck(after["ok"]["finished_at"] == before["ok"]["finished_at"],
       "★ success 的 finished_at 不变（成功统计依赖它）")
    ck(after["ok"]["object_id"] == "obj-1", "success 的 object_id 不变")

    print("\n[2] 幂等：再调一次应为 0，且一条都不改")
    snap = {t: st.get_task(i)["status"] for t, i in ids.items()}
    n2 = st.reset_running_tasks()
    ck(n2 == 0, "第二次返回 0", "got %r" % n2)
    ck({t: st.get_task(i)["status"] for t, i in ids.items()} == snap,
       "第二次调用没有改动任何状态")

    print("\n[3] 空库不炸")
    st2 = Storage(os.path.join(tmp, "empty.db"))
    ck(st2.reset_running_tasks() == 0, "空表返回 0")

    print("\n[4] 多条 running 同时收尾")
    tmp3 = tempfile.mkdtemp(prefix="rev40b_")
    st3 = Storage(os.path.join(tmp3, "t.db"))
    a, _ = st3.enqueue_task(F, r"D:\inbox\r1.mp4", "r1.mp4", 1, "fp-r1", "t1", 0)
    b, _ = st3.enqueue_task(F, r"D:\inbox\r2.mp4", "r2.mp4", 2, "fp-r2", "t2", 0)
    c, _ = st3.enqueue_task(F, r"D:\inbox\s1.mp4", "s1.mp4", 3, "fp-s1", "t3", 0)
    st3.set_task_status(a, status="running", started=True)
    st3.set_task_status(b, status="running", started=True)
    st3.set_task_status(c, status="success", finished=True)
    ck(st3.reset_running_tasks() == 2, "返回 2（两条 running）")
    ck(st3.get_task(c)["status"] == "success", "★ 同库里 success 仍未被污染")

    print("\n合计 PASS=%d FAIL=%d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
