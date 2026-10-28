# -*- coding: utf-8 -*-
"""仿真验证 fix_rev40_need_review.py：造与现场同构的脏数据，确认修复口径正确。

现场形状：306 条 need_review，其中 259 条其实已发布（源文件已归档进 _done/），
其余 47 条没有任何「已发出」证据。要求：
  - 259 条恢复 success
  - 47 条一条都不许动（保守原则）
  - 幂等：再跑一次 0 改动
  - 备份文件生成
"""
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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
    tmp = tempfile.mkdtemp(prefix="rev40fix_")
    db = os.path.join(tmp, "sphgj.db")
    inbox = os.path.join(tmp, "publish_inbox")
    done_dirs = [os.path.join(inbox, "_done"),           # _archive() 实际写的位置
                 os.path.join(inbox, "免税仓", "_done")]  # 现场描述的 <店名>/_done
    for d in done_dirs:
        os.makedirs(d, exist_ok=True)

    st = Storage(db)
    F = "finderA"
    plan = []   # (tag, has_file, kind)

    # 200 条：成功 + 归档文件（_done 根级），带 object_id / step=done / archived_name
    for i in range(200):
        plan.append(("ok_file_root_%03d" % i, done_dirs[0], "full"))
    # 59 条：成功 + 归档文件（店铺子目录），只有 _done 文件 + archived_name，无 object_id
    for i in range(59):
        plan.append(("ok_file_shop_%03d" % i, done_dirs[1], "file_only"))
    # 25 条：失败（step=exception，有 last_error），无任何成功痕迹
    for i in range(25):
        plan.append(("failed_%03d" % i, None, "failed"))
    # 20 条：中断前还在 pending / running，无成功痕迹
    for i in range(20):
        plan.append(("pending_%03d" % i, None, "pending"))
    # 2 条：success 但 finished_at 被清空（测 --fill-finished-at）
    for i in range(2):
        plan.append(("ok_no_fin_%03d" % i, done_dirs[0], "no_fin"))

    ids = {}
    for i, (tag, d, kind) in enumerate(plan):
        fn = tag + ".mp4"
        src = os.path.join(tmp, "src", fn)
        os.makedirs(os.path.dirname(src), exist_ok=True)
        with open(src, "wb") as f:
            f.write(b"x" * 10)
        tid, created = st.enqueue_task(F, src, fn, 10, "fp-" + tag, "标题" + tag, 0)
        assert created, tag
        ids[tag] = (tid, kind, d)

    # 造出「bug 之后」的现场：全部 need_review
    c = sqlite3.connect(db)
    for tag, (tid, kind, d) in ids.items():
        if kind == "full":
            arch = "2026-10-20发布-标题-%s.mp4" % tag
            open(os.path.join(d, arch), "wb").write(b"x" * 10)
            c.execute("""UPDATE publish_tasks SET status='need_review', step='done',
                         object_id=?, finished_at=?, archived_name=?,
                         last_error='进程中断,不确定是否已发出,请到视频号后台核对后再决定重试'
                         WHERE id=?""", ("obj-" + tag, "2026-10-20T03:56:06", arch, tid))
        elif kind == "file_only":
            arch = "2026-10-21发布-标题-%s.mp4" % tag
            open(os.path.join(d, arch), "wb").write(b"x" * 10)
            # 没有 object_id / 没有 step=done，只有 archived_name + 归档文件 -> D 类证据
            c.execute("""UPDATE publish_tasks SET status='need_review', step='upload',
                         object_id='', finished_at=NULL, archived_name=?,
                         last_error='进程中断,不确定是否已发出,请到视频号后台核对后再决定重试'
                         WHERE id=?""", (arch, tid))
        elif kind == "failed":
            c.execute("""UPDATE publish_tasks SET status='need_review', step='exception',
                         last_error='RuntimeError: 上传超时' WHERE id=?""", (tid,))
        elif kind == "pending":
            c.execute("""UPDATE publish_tasks SET status='need_review', step='prepare',
                         last_error='' WHERE id=?""", (tid,))
        elif kind == "no_fin":
            arch = "2026-10-22发布-标题-%s.mp4" % tag
            open(os.path.join(d, arch), "wb").write(b"x" * 10)
            c.execute("""UPDATE publish_tasks SET status='need_review', step='done',
                         object_id=?, finished_at=NULL, archived_name=?,
                         last_error='进程中断,不确定是否已发出,请到视频号后台核对后再决定重试'
                         WHERE id=?""", ("obj-" + tag, arch, tid))
    c.commit()
    dist = dict(c.execute("SELECT status,COUNT(*) FROM publish_tasks GROUP BY status"))
    print("\n[0] 仿真现场：", dist)
    ck(dist.get("need_review") == 306, "造出 306 条 need_review", str(dist))
    c.close()

    script = os.path.join(ROOT, "tools", "fix_rev40_need_review.py")
    csvp = os.path.join(tmp, "detail.csv")

    print("\n[1] dry-run：必须一条都不改")
    out = subprocess.run([sys.executable, script, "--db", db, "--inbox", inbox,
                          "--csv", csvp], capture_output=True, text=True, encoding="utf-8")
    c = sqlite3.connect(db)
    still = c.execute("SELECT COUNT(*) FROM publish_tasks WHERE status='need_review'").fetchone()[0]
    c.close()
    ck(out.returncode == 0, "退出码 0", out.stderr[-300:])
    ck(still == 306, "dry-run 后仍是 306 条 need_review（没有偷写）", "got %d" % still)
    ck("DRY-RUN" in out.stdout, "输出里明确提示 DRY-RUN")
    ck("可判定为「其实已发布成功」: 261 条" in out.stdout,
       "判定出 261 条可恢复（200 full + 59 file_only + 2 no_fin）", out.stdout[-800:])
    ck("保留 need_review 待人工核对: 45 条" in out.stdout,
       "判定出 45 条证据不足（25 failed + 20 pending）", out.stdout[-800:])
    ck(os.path.isfile(csvp), "导出明细 CSV")

    print("\n[2] --apply：真改")
    out2 = subprocess.run([sys.executable, script, "--db", db, "--inbox", inbox,
                           "--apply", "--fill-finished-at"],
                          capture_output=True, text=True, encoding="utf-8")
    ck(out2.returncode == 0, "退出码 0", out2.stderr[-400:])
    c = sqlite3.connect(db)
    dist2 = dict(c.execute("SELECT status,COUNT(*) FROM publish_tasks GROUP BY status").fetchall())
    ck(dist2.get("success") == 261, "success 261 条", str(dist2))
    ck(dist2.get("need_review") == 45, "need_review 只剩 45 条", str(dist2))

    # 25 条 failed 的 last_error 必须原样保留
    kept_err = c.execute("""SELECT COUNT(*) FROM publish_tasks
                            WHERE status='need_review' AND last_error='RuntimeError: 上传超时'""").fetchone()[0]
    ck(kept_err == 25, "★ 保留的 failed 记录 last_error 未被覆盖", str(kept_err))
    # 恢复的记录里假提示必须清掉
    bad = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='success'
                       AND last_error LIKE '%进程中断%'""").fetchone()[0]
    ck(bad == 0, "★ 恢复的 success 不再带「进程中断」假提示", str(bad))
    # --fill-finished-at 至少补上 2 条空的
    filled = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='success'
                          AND finished_at IS NOT NULL AND finished_at<>''""").fetchone()[0]
    ck(filled == 261, "★ finished_at 全部有值（--fill-finished-at 生效）", str(filled))
    c.close()

    bk = [d for d in os.listdir(tmp) if d.startswith("backup-rev40-")]
    ck(len(bk) == 1, "生成了 1 个备份目录", str(bk))

    print("\n[3] 幂等：再跑一次应 0 改动")
    out3 = subprocess.run([sys.executable, script, "--db", db, "--inbox", inbox, "--apply"],
                          capture_output=True, text=True, encoding="utf-8")
    ck(out3.returncode == 0, "第二次退出码 0")
    c = sqlite3.connect(db)
    dist3 = dict(c.execute("SELECT status,COUNT(*) FROM publish_tasks GROUP BY status").fetchall())
    c.close()
    ck(dist3 == dist2, "★ 第二次运行后状态分布不变", "%s vs %s" % (dist3, dist2))

    print("\n[4] 全新空库不炸")
    tmp2 = tempfile.mkdtemp(prefix="rev40empty_")
    st2 = Storage(os.path.join(tmp2, "e.db"))
    out4 = subprocess.run([sys.executable, script, "--db", os.path.join(tmp2, "e.db"),
                           "--inbox", tmp2], capture_output=True, text=True, encoding="utf-8")
    ck(out4.returncode == 0 and "没有需要修复的记录" in out4.stdout,
       "空库直接提示无事可做", out4.stdout[-300:])

    print("\n合计 PASS=%d FAIL=%d" % (PASS, FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
