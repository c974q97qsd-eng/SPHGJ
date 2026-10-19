# -*- coding: utf-8 -*-
"""rev25-1: storage 增加「自动发作品」两表 + CRUD。

publish_titles: 标题池(按视频号 finder_id 归属;finder_id='' 为通用池)
  一条标题只用一次(用完即弃):used_count=1 表示已用,发布失败会归还(重置为 0)
publish_tasks : 发布队列与历史
  (finder_id, file_fp) 唯一 -> 同一文件永不重复入队(幂等)
  status: pending / running / success / failed / skipped / need_review
"""
import io, sys

P = r"D:\SPHGJ\backend\storage.py"
s = io.open(P, encoding="utf-8").read()
orig_len = len(s)

# ---------------- 1) DDL ----------------
OLD = '        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀'
NEW = '''        # 自动发作品:标题池 + 发布队列(归属一律 finder_id,与作品/评论同口径)
        c.execute("""CREATE TABLE IF NOT EXISTS publish_titles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT DEFAULT '',
            content TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            used_task_id INTEGER DEFAULT 0,
            last_used_at TEXT,
            created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS publish_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finder_id TEXT,
            account_id TEXT,
            src_path TEXT,
            file_name TEXT,
            file_size INTEGER DEFAULT 0,
            file_fp TEXT,
            title TEXT,
            title_id INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            step TEXT DEFAULT '',
            attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            object_id TEXT DEFAULT '',
            export_id TEXT DEFAULT '',
            queued_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(finder_id, file_fp))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_tasks_status ON publish_tasks(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_tasks_finder ON publish_tasks(finder_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pub_titles_finder ON publish_titles(finder_id)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 启动时收敛 WAL,避免长期运行 WAL 膨胀'''
assert s.count(OLD) == 1, "DDL 锚点未唯一命中"
s = s.replace(OLD, NEW, 1)

# ---------------- 2) 类内追加方法(文件末尾,保持 4 空格缩进仍在 class Storage 内) ----------------
CRUD = '''

    # ==================== 自动发作品(发布队列 + 标题池) ====================

    # ---------- 标题池 ----------
    def add_titles(self, contents, finder_id=""):
        """批量加标题(一行一条)。返回实际新增条数;空行/同池内内容重复的跳过。"""
        now = datetime.now().isoformat()
        c = self._conn()
        try:
            exist = {r[0] for r in c.execute(
                "SELECT content FROM publish_titles WHERE finder_id=?", (finder_id or "",))}
            n = 0
            for raw in contents:
                t = (raw or "").strip()
                if not t or t in exist:
                    continue
                c.execute("""INSERT INTO publish_titles(finder_id,content,enabled,used_count,
                             used_task_id,last_used_at,created_at) VALUES(?,?,1,0,0,NULL,?)""",
                          (finder_id or "", t, now))
                exist.add(t)
                n += 1
            c.commit()
            return n
        finally:
            c.close()

    def query_titles(self, finder_id=None, include_used=True, limit=500):
        """查标题池。finder_id=None 查全部;''=只看通用池。未用的排前面。"""
        c = self._conn()
        try:
            where, args = [], []
            if finder_id is not None:
                where.append("finder_id=?"); args.append(finder_id)
            if not include_used:
                where.append("used_count=0")
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            rows = c.execute(
                f"""SELECT id,finder_id,content,enabled,used_count,used_task_id,last_used_at,created_at
                    FROM publish_titles{clause} ORDER BY used_count ASC, id ASC LIMIT ?""",
                args + [limit]).fetchall()
            cols = ("id", "finder_id", "content", "enabled", "used_count",
                    "used_task_id", "last_used_at", "created_at")
            return [dict(zip(cols, r)) for r in rows]
        finally:
            c.close()

    def title_pool_stats(self):
        """各池标题余量:{"<finder_id或''>": {"total":n,"unused":m}}。"""
        c = self._conn()
        try:
            out = {}
            for fid, total, unused in c.execute(
                    """SELECT finder_id, COUNT(*), SUM(CASE WHEN used_count=0 THEN 1 ELSE 0 END)
                       FROM publish_titles WHERE enabled=1 GROUP BY finder_id"""):
                out[fid or ""] = {"total": total or 0, "unused": unused or 0}
            return out
        finally:
            c.close()

    def delete_title(self, title_id):
        c = self._conn()
        try:
            c.execute("DELETE FROM publish_titles WHERE id=?", (title_id,))
            c.commit()
        finally:
            c.close()

    def clear_titles(self, finder_id=None, only_used=False):
        """清标题池。only_used=True 只清已用过的(方便补新标题)。"""
        c = self._conn()
        try:
            where, args = [], []
            if finder_id is not None:
                where.append("finder_id=?"); args.append(finder_id)
            if only_used:
                where.append("used_count>0")
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            n = c.execute(f"SELECT COUNT(*) FROM publish_titles{clause}", args).fetchone()[0]
            c.execute(f"DELETE FROM publish_titles{clause}", args)
            c.commit()
            return n
        finally:
            c.close()

    def take_title(self, finder_id):
        """取一条可用标题并立即标记已用(一条一用,用完即弃)。

        优先该视频号专属池,取完自动回落到通用池(finder_id='')。
        返回 (title_id, content);两个池都没有可用标题时返回 (0, '')。
        """
        now = datetime.now().isoformat()
        c = self._conn()
        try:
            for pool in (finder_id or "", ""):
                if not pool and finder_id:
                    pass  # 仍要试通用池
                row = c.execute(
                    """SELECT id,content FROM publish_titles
                       WHERE finder_id=? AND enabled=1 AND used_count=0
                       ORDER BY id ASC LIMIT 1""", (pool,)).fetchone()
                if row:
                    c.execute("UPDATE publish_titles SET used_count=1, last_used_at=? WHERE id=?",
                              (now, row[0]))
                    c.commit()
                    return int(row[0]), row[1]
            return 0, ""
        finally:
            c.close()

    def release_title(self, title_id):
        """发布失败时把标题归还池子(没真正发出去,标题还能给下一条用)。"""
        if not title_id:
            return
        c = self._conn()
        try:
            c.execute("UPDATE publish_titles SET used_count=0, used_task_id=0 WHERE id=?",
                      (int(title_id),))
            c.commit()
        finally:
            c.close()

    def bind_title_task(self, title_id, task_id):
        c = self._conn()
        try:
            c.execute("UPDATE publish_titles SET used_task_id=? WHERE id=?",
                      (int(task_id), int(title_id)))
            c.commit()
        finally:
            c.close()

    # ---------- 发布队列 ----------
    def enqueue_task(self, finder_id, src_path, file_name, file_size, file_fp, title="",
                     title_id=0, account_id=""):
        """入队(幂等)。返回 (task_id, created)。同 (finder_id,file_fp) 已存在则不新建。"""
        c = self._conn()
        try:
            row = c.execute(
                "SELECT id FROM publish_tasks WHERE finder_id=? AND file_fp=?",
                (finder_id, file_fp)).fetchone()
            if row:
                return int(row[0]), False
            cur = c.execute(
                """INSERT INTO publish_tasks(finder_id,account_id,src_path,file_name,file_size,
                     file_fp,title,title_id,status,step,attempts,last_error,queued_at)
                   VALUES(?,?,?,?,?,?,?,?,'pending','',0,'',?)""",
                (finder_id, account_id, src_path, file_name, file_size, file_fp,
                 title, int(title_id or 0), datetime.now().isoformat()))
            c.commit()
            return int(cur.lastrowid), True
        finally:
            c.close()

    def get_task(self, task_id):
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()
            if not r:
                return None
            return dict(zip([d[0] for d in c.execute(
                "SELECT * FROM publish_tasks LIMIT 0").description], r))
        finally:
            c.close()

    def query_tasks(self, status=None, finder_id=None, limit=200, offset=0):
        """查发布队列,返回 (rows, total)。status 可传 'pending,running' 这种逗号串。"""
        c = self._conn()
        try:
            where, args = [], []
            if status:
                flags = [x.strip() for x in str(status).split(",") if x.strip()]
                if flags:
                    where.append("status IN (" + ",".join("?" * len(flags)) + ")")
                    args.extend(flags)
            if finder_id:
                where.append("finder_id=?"); args.append(finder_id)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            total = c.execute(f"SELECT COUNT(*) FROM publish_tasks{clause}", args).fetchone()[0]
            rows = c.execute(
                f"""SELECT * FROM publish_tasks{clause}
                    ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                             id ASC LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
            cols = [d[0] for d in c.execute("SELECT * FROM publish_tasks LIMIT 0").description]
            return [dict(zip(cols, r)) for r in rows], total
        finally:
            c.close()

    def set_task_status(self, task_id, status=None, step=None, error=None, object_id=None,
                        export_id=None, account_id=None, started=False, finished=False,
                        attempts=None):
        sets, args = [], []
        for col, val in (("status", status), ("step", step), ("last_error", error),
                         ("object_id", object_id), ("export_id", export_id),
                         ("account_id", account_id), ("attempts", attempts)):
            if val is not None:
                sets.append(f"{col}=?"); args.append(val)
        if started:
            sets.append("started_at=?"); args.append(datetime.now().isoformat())
        if finished:
            sets.append("finished_at=?"); args.append(datetime.now().isoformat())
        if not sets:
            return
        c = self._conn()
        try:
            c.execute(f"UPDATE publish_tasks SET {','.join(sets)} WHERE id=?",
                      args + [int(task_id)])
            c.commit()
        finally:
            c.close()

    def update_task_title(self, task_id, title):
        c = self._conn()
        try:
            c.execute("UPDATE publish_tasks SET title=? WHERE id=?", (title or "", int(task_id)))
            c.commit()
        finally:
            c.close()

    def reset_task(self, task_id, clear_error=True):
        """把任务重置回 pending(人工点「重试」用)。"""
        c = self._conn()
        try:
            c.execute("""UPDATE publish_tasks SET status='pending', step='', started_at=NULL,
                         finished_at=NULL""" + (", last_error=''" if clear_error else "") +
                      " WHERE id=?", (int(task_id),))
            c.commit()
        finally:
            c.close()

    def reset_running_tasks(self):
        """启动时收尾:上次进程死在 running 的任务不能自动重发(怕实际已发出去),
        统一转 need_review 等人工确认。返回受影响条数。"""
        c = self._conn()
        try:
            n = c.execute("SELECT COUNT(*) FROM publish_tasks WHERE status='running'").fetchone()[0]
            if n:
                c.execute("""UPDATE publish_tasks SET status='need_review',
                             last_error=COALESCE(NULLIF(last_error,''),
                               '进程中断,不确定是否已发出,请到视频号后台核对后再决定重试')""")
            c.commit()
            return n
        finally:
            c.close()

    def daily_success_count(self, finder_id, date_str):
        """某视频号某天成功发布条数(date_str = YYYY-MM-DD,按 finished_at 统计)。"""
        c = self._conn()
        try:
            return c.execute(
                """SELECT COUNT(*) FROM publish_tasks
                   WHERE finder_id=? AND status='success' AND finished_at LIKE ?""",
                (finder_id, (date_str or "") + "%")).fetchone()[0]
        finally:
            c.close()

    def last_success_at(self, finder_id):
        c = self._conn()
        try:
            r = c.execute(
                """SELECT MAX(finished_at) FROM publish_tasks
                   WHERE finder_id=? AND status='success'""", (finder_id,)).fetchone()
            return (r[0] if r else "") or ""
        finally:
            c.close()

    def delete_task(self, task_id):
        c = self._conn()
        try:
            c.execute("DELETE FROM publish_tasks WHERE id=?", (int(task_id),))
            c.commit()
        finally:
            c.close()
'''

s = s.rstrip("\n") + CRUD
io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------- 3) 自校验 ----------------
chk = io.open(P, encoding="utf-8").read()
must = ["CREATE TABLE IF NOT EXISTS publish_titles", "CREATE TABLE IF NOT EXISTS publish_tasks",
        "def take_title", "def enqueue_task", "def reset_running_tasks",
        "def daily_success_count", "def release_title"]
missing = [m for m in must if m not in chk]
print(f"size {orig_len} -> {len(chk)}")
print("SELFCHECK", "PASS" if not missing else f"FAIL missing={missing}")
sys.exit(1 if missing else 0)
