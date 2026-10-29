"""rev58 一次性补丁:storage.py(排序口径 + 各账号任务计数 + 标题池排序)。

为什么用补丁脚本而不是多条 Edit:同一个文件连发多条 Edit 时,实测只有最后一条
真正落盘(2026-09-14 踩过)。这里逐条 assert 唯一命中,再一次写回。
"""
import io
import os

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                 "backend", "storage.py")
P = os.path.abspath(P)


def apply(src, patches):
    for tag, old, new in patches:
        n = src.count(old)
        assert n == 1, f"[{tag}] 命中 {n} 次,期望 1 次"
        src = src.replace(old, new, 1)
        print(f"  ok  {tag}")
    return src


s = io.open(P, encoding="utf-8").read()

# ---- 1. query_tasks 增加 order 参数(界面排序口径,不能动内部调度依赖的旧序) ----
OLD_SIG = '''    def query_tasks(self, status=None, finder_id=None, limit=200, offset=0):
        """查发布队列,返回 (rows, total)。status 可传 'pending,running' 这种逗号串。"""
'''
NEW_SIG = '''    def query_tasks(self, status=None, finder_id=None, limit=200, offset=0, order=None):
        """查发布队列,返回 (rows, total)。status 可传 'pending,running' 这种逗号串。

        order 是【界面】的排序口径,只认白名单:
            "queue"   发布中固定置顶,其余按入队 id 倒序(新的在前)
            "history" 按完成/开始/入队时间倒序(最新的在前),同一秒的按 id 倒序
            其它/不传 保持旧序(运行中 > 待发 > 其余,组内 id 升序)

        为什么不直接把默认序改成倒序:内部调度 _tick()/fill_missing_titles() 都用
        不带 order 的调用,它们依赖「旧任务先发」。界面想看到最新的在最上面,
        调度却必须从最老的开始,两种需求共用一条 SQL 会互相打架。
        """
        order_sql = {
            "queue": "CASE status WHEN 'running' THEN 0 ELSE 1 END, id DESC",
            "history": "COALESCE(finished_at, started_at, queued_at) DESC, id DESC",
        }.get(str(order or "").strip(),
              "CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, id ASC")
'''
s = apply(s, [("query_tasks 签名/文档", OLD_SIG, NEW_SIG)])

# ---- 2. 用 order_sql 组装 ORDER BY ----
OLD_ORD = '''            rows = c.execute(
                f"""SELECT * FROM publish_tasks{clause}
                    ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                             id ASC LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()'''
NEW_ORD = '''            rows = c.execute(
                f"""SELECT * FROM publish_tasks{clause}
                    ORDER BY {order_sql} LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()'''
s = apply(s, [("query_tasks ORDER BY", OLD_ORD, NEW_ORD)])

# ---- 3. 新增各视频号任务计数(账号卡片上的数字) ----
ANCHOR = '''    def set_task_status(self, task_id, status=None, step=None, error=None, object_id=None,'''
NEW_METHOD = '''    def task_counts_by_finder(self):
        """各视频号的任务数:{"<finder_id>": {"pending": n, "success": n, ...}}。

        发布管理页的「账号选项卡片」要在每个账号上显示条数(待发 N / 已发 M),
        一条 GROUP BY 就能拿到全部,不用前端按账号逐个请求。
        没有任何任务的视频号不出现在返回里。
        """
        c = self._conn()
        try:
            out = {}
            for fid, st, n in c.execute(
                    "SELECT finder_id, status, COUNT(*) FROM publish_tasks "
                    "GROUP BY finder_id, status"):
                out.setdefault(fid or "", {})[st] = n
            return out
        finally:
            c.close()

'''
s = apply(s, [("task_counts_by_finder", ANCHOR, NEW_METHOD + ANCHOR)])

# ---- 4. 标题池:同一组内最新加的排在前面 ----
OLD_T = '''                    FROM publish_titles{clause} ORDER BY used_count ASC, id ASC LIMIT ?""",'''
NEW_T = '''                    FROM publish_titles{clause} ORDER BY used_count ASC, id DESC LIMIT ?""",'''
s = apply(s, [("query_titles 排序", OLD_T, NEW_T)])

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---- 自校验 ----
chk = io.open(P, encoding="utf-8").read()
assert "def query_tasks(self, status=None, finder_id=None, limit=200, offset=0, order=None):" in chk
assert "ORDER BY {order_sql} LIMIT ? OFFSET ?" in chk
assert "def task_counts_by_finder(self):" in chk
assert "ORDER BY used_count ASC, id DESC" in chk
assert "ORDER BY used_count ASC, id ASC" not in chk
print("SELFCHECK PASS")
