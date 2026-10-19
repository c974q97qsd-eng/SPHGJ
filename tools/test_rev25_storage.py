# -*- coding: utf-8 -*-
"""rev25 storage 自测:标题池一条一用 + 队列幂等 + 复位语义。"""
import os, sys, tempfile

sys.path.insert(0, r"D:\SPHGJ")
from backend.storage import Storage

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "t.db")
st = Storage(db)
st2 = Storage(db)  # 幂等:重复打开同一库(表已存在)不应炸
assert st2.db_path == db

F1, F2 = "finderA", "finderB"

# ---- 标题池:专属优先,取完回落通用池,一条一用 ----
n = st.add_titles(["标题1", "标题2", "  ", "标题1"], F1)
assert n == 2, f"去重/空行处理错: {n}"
assert st.add_titles(["通用1", "通用2"], "") == 2

pool = st.title_pool_stats()
assert pool[F1] == {"total": 2, "unused": 2}, pool
assert pool[""] == {"total": 2, "unused": 2}, pool

t1 = st.take_title(F1)
t2 = st.take_title(F1)
t3 = st.take_title(F1)   # 专属用完 -> 通用池
t4 = st.take_title(F1)
t5 = st.take_title(F1)   # 全空
assert t1[1] == "标题1" and t2[1] == "标题2", (t1, t2)
assert t3[1] in ("通用1", "通用2"), t3
assert t4[1] in ("通用1", "通用2") and t4[1] != t3[1], (t3, t4)
assert t5 == (0, ""), t5
assert st.title_pool_stats()[F1]["unused"] == 0

# ---- 发布失败归还标题(还能给下一条用) ----
st.release_title(t1[0])
assert st.title_pool_stats()[F1]["unused"] == 1
assert st.take_title(F1)[1] == "标题1"

# ---- 队列幂等:同 (finder, fp) 只入一次 ----
tid, created = st.enqueue_task(F1, r"D:\inbox\a.mp4", "a.mp4", 1000, "fp-a", "标题1", 0)
assert created and tid > 0
tid2, created2 = st.enqueue_task(F1, r"D:\inbox\a.mp4", "a.mp4", 1000, "fp-a")
assert tid2 == tid and not created2, "同文件不应重复入队"
# 不同视频号同文件 -> 允许(可能故意分发到多个号)
tid3, created3 = st.enqueue_task(F2, r"D:\inbox\a.mp4", "a.mp4", 1000, "fp-a")
assert created3 and tid3 != tid

# ---- 状态流转 ----
st.set_task_status(tid, status="running", step="upload", started=True, account_id="acc-x")
t = st.get_task(tid)
assert t["status"] == "running" and t["step"] == "upload" and t["started_at"], t
rows, total = st.query_tasks(status="running")
assert total == 1 and rows[0]["id"] == tid

# 启动收尾:running -> need_review(不自动重发)
assert st.reset_running_tasks() == 1
assert st.get_task(tid)["status"] == "need_review"

# ---- 日计数 / 首次发布 ----
st.set_task_status(tid, status="success", step="done", object_id="obj-1",
                   export_id="exp-1", finished=True)
import datetime
today = datetime.date.today().strftime("%Y-%m-%d")
assert st.daily_success_count(F1, today) == 1
assert st.daily_success_count(F1, "1999-01-01") == 0
assert st.last_success_at(F1), "last_success_at 应非空"

# ---- 人工重试 ----
st.reset_task(tid)
t = st.get_task(tid)
assert t["status"] == "pending" and t["last_error"] == "", t

print("ALL PASS  (db:", db, ")")
