# -*- coding: utf-8 -*-
"""rev40: 修 Storage.reset_running_tasks() 漏 WHERE 把全表刷成 need_review。

只改这一个方法，不动别处。脚本幂等：已应用过就跳过（避免误跑时满屏 assert）。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "backend", "storage.py")

OLD = '''    def reset_running_tasks(self):
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
'''

NEW = '''    def reset_running_tasks(self):
        """启动时收尾:上次进程死在 running 的任务不能自动重发(怕实际已发出去),
        统一转 need_review 等人工确认。返回受影响条数。
        rev40: 只更新 running 的 id。旧版 UPDATE 没有 WHERE,只要启动时存在 1 条 running,
        就会把全表(含已 success / failed 的历史记录)统统刷成 need_review,
        导致「已发布」被误判成「待核对」,成功统计失真。"""
        c = self._conn()
        try:
            ids = [r[0] for r in
                   c.execute("SELECT id FROM publish_tasks WHERE status='running'").fetchall()]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            c.execute(f"""UPDATE publish_tasks SET status='need_review',
                          last_error=COALESCE(NULLIF(last_error,''),
                            '进程中断,不确定是否已发出,请到视频号后台核对后再决定重试')
                          WHERE id IN ({marks})""", ids)
            c.commit()
            return len(ids)
        finally:
            c.close()
'''


def main():
    s = io.open(P, encoding="utf-8").read()

    if "rev40: 只更新 running 的 id" in s:
        print("rev40 已应用过（storage.py 里已有 rev40 注释），跳过。")
        return 0

    assert s.count(OLD) == 1, "锚点命中 %d 次，中止" % s.count(OLD)
    s = s.replace(OLD, NEW, 1)
    io.open(P, "w", encoding="utf-8", newline="").write(s)

    # 自校验
    t = io.open(P, encoding="utf-8").read()
    checks = [
        ("新注释已落地", "rev40: 只更新 running 的 id" in t),
        ("不再有裸 UPDATE", "WHERE id IN ({marks})" in t),
        ("返回值改为 len(ids)", "return len(ids)" in t),
        ("旧 COUNT(*) 写法已消失",
         "SELECT COUNT(*) FROM publish_tasks WHERE status='running'" not in t),
    ]
    ok = True
    for name, cond in checks:
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    import py_compile
    try:
        py_compile.compile(P, doraise=True)
        print("  PASS 语法检查")
    except Exception as e:
        print("  FAIL 语法检查:", e)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
