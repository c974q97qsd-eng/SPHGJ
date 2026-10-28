# -*- coding: utf-8 -*-
"""rev40 一次性数据修复：把被 reset_running_tasks 漏 WHERE 误刷成 need_review 的
「其实已发布成功」的记录恢复为 success。

背景
----
旧版 reset_running_tasks() 的 UPDATE 没有 WHERE：只要启动时存在 1 条 running，
全表（含已 success / failed / pending 的历史记录）统统被刷成 need_review。
need_review 在代码里**只有这一个写入点**，所以库里所有 need_review 都是它刷出来的。

修复原则（保守）
----------------
只回滚**有确定性证据证明曾发布成功**的记录，其余一律保留 need_review 让人工核对。
证据（任一命中即可，全部来自发布成功路径才写的字段）：
  A. archived_name 非空
  B. object_id 非空
  C. step == 'done'
  D. publish_inbox 下任一 _done/ 目录里存在对应归档文件
     （按 archived_name / file_name 精确匹配，或以 "-<file_name>" 结尾匹配，
      兼容 rename_after_publish 的「日期发布-标题-原名」命名）
无法证明的一律不动 —— 怕的是把真正没发出去的记录标成成功，那比误标更糟。

安全
----
* 默认 dry-run，只打印不写库；必须显式 --apply 才修改。
* --apply 前用 sqlite backup API 备份整库到 data/backup-rev40-<时间戳>/。
* 幂等：已恢复的记录不再是 need_review，重复跑不会重复改。
* 不动 config.json / profiles / publish_inbox 里的任何文件（只读目录用来匹配）。
* --fill-finished-at：仅当 success 记录 finished_at 为空时，用归档文件 mtime 兜底
  （成功统计 daily_success_count 依赖 finished_at）。默认关闭。

用法
----
  python tools/fix_rev40_need_review.py                    # dry-run，打印方案
  python tools/fix_rev40_need_review.py --apply            # 真改（先自动备份）
  python tools/fix_rev40_need_review.py --db X --inbox Y   # 指定库与 inbox
  python tools/fix_rev40_need_review.py --csv out.csv      # 导出明细
"""
import argparse
import io
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 旧 bug 写进去的提示文案（恢复成功时要清掉；别的内容不动）
BUG_HINT = "进程中断,不确定是否已发出,请到视频号后台核对后再决定重试"

EVID_TAG = {
    "archived_name": "A 归档名非空",
    "object_id": "B 作品ID非空",
    "step_done": "C step=done",
    "done_file": "D _done/有归档文件",
}


def norm(s):
    return (s or "").strip()


def build_done_index(inbox):
    """扫描 inbox 下所有 _done/ 目录，返回 {basename: [full_path, ...]}。

    两种结构都支持：
      inbox/_done/<name>            （_archive() 实际写的位置）
      inbox/<店铺>/_done/<name>      （旧版/手工整理的结构）
    """
    idx = {}
    if not inbox or not os.path.isdir(inbox):
        return idx, 0
    dirs = 0
    for cur, subs, files in os.walk(inbox):
        if os.path.basename(cur) != "_done":
            continue
        dirs += 1
        for fn in files:
            idx.setdefault(fn, []).append(os.path.join(cur, fn))
    return idx, dirs


def match_done_file(task, idx):
    """返回命中的归档文件路径（无命中返回 ""）。"""
    an = norm(task.get("archived_name"))
    fn = norm(task.get("file_name"))
    if an:
        hit = idx.get(an)
        if hit:
            return hit[0]
    if fn:
        hit = idx.get(fn)
        if hit:
            return hit[0]
        # rename_after_publish: 「日期发布-标题-原名」
        for name, paths in idx.items():
            if name.endswith("-" + fn) or name.endswith(fn):
                return paths[0]
    return ""


def judge(task, idx):
    """返回 (是否恢复, 证据标签列表, 归档文件路径)。"""
    tags = []
    fp = ""
    if norm(task.get("archived_name")):
        tags.append("archived_name")
    if norm(task.get("object_id")):
        tags.append("object_id")
    if norm(task.get("step")).lower() == "done":
        tags.append("step_done")
    fp = match_done_file(task, idx)
    if fp:
        tags.append("done_file")
    return bool(tags), tags, fp


def table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def status_dist(c):
    rows = c.execute("SELECT status,COUNT(*) FROM publish_tasks "
                     "GROUP BY status ORDER BY 2 DESC").fetchall()
    return {r[0]: r[1] for r in rows}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "sphgj.db"))
    ap.add_argument("--inbox", default=os.path.join(ROOT, "data", "publish_inbox"))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只 dry-run）")
    ap.add_argument("--csv", default="", help="把明细导出到该 CSV 路径")
    ap.add_argument("--fill-finished-at", action="store_true",
                    help="success 但 finished_at 为空时用归档文件 mtime 兜底")
    ap.add_argument("--quiet", action="store_true", help="只打印汇总")
    a = ap.parse_args(argv)

    db, inbox = a.db, a.inbox
    print("库文件 : %s" % db)
    print("inbox  : %s" % inbox)
    if not os.path.isfile(db):
        print("!! 库不存在，退出")
        return 2
    if not os.path.isdir(inbox):
        print("!! inbox 目录不存在（D 类证据将全部失效，其余证据照常判定）")

    idx, ndirs = build_done_index(inbox)
    print("_done 目录 %d 个，归档文件 %d 个（去重后 %d 名）"
          % (ndirs, sum(len(v) for v in idx.values()), len(idx)))

    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        if not table_exists(c, "publish_tasks"):
            print("!! 库里没有 publish_tasks 表，退出")
            return 2

        dist = status_dist(c)
        total = sum(dist.values())
        print("\n当前状态分布（共 %d 条）:" % total)
        for k, v in dist.items():
            print("   %-14s %d" % (k, v))

        nr = [dict(r) for r in c.execute(
            "SELECT * FROM publish_tasks WHERE status='need_review' ORDER BY id")]
        print("\nneed_review 共 %d 条" % len(nr))
        if not nr:
            print("→ 没有需要修复的记录（可能是全新库，或已修复过）。无事可做。")
            return 0

        recover, keep = [], []
        for t in nr:
            ok, tags, fp = judge(t, idx)
            (recover if ok else keep).append((t, tags, fp))

        from collections import Counter
        tagc = Counter()
        for t, tags, fp in recover:
            tagc["+".join(sorted(tags))] += 1
        print("\n可判定为「其实已发布成功」: %d 条" % len(recover))
        for k, v in tagc.most_common():
            print("   %-40s %d" % (k, v))
        print("证据不足、保留 need_review 待人工核对: %d 条" % len(keep))

        if keep and not a.quiet:
            print("\n保留明细（前 15 条，按 id）:")
            for t, tags, fp in keep[:15]:
                print("   id=%-5s %-40s step=%-10s err=%s"
                      % (t.get("id"), (t.get("file_name") or "")[:40],
                         norm(t.get("step"))[:10], norm(t.get("last_error"))[:40]))
            if len(keep) > 15:
                print("   … 其余 %d 条见 --csv" % (len(keep) - 15))

        miss_fin = [t for t, tags, fp in recover if not norm(t.get("finished_at"))]
        print("\n恢复后 finished_at 会为空的: %d 条%s"
              % (len(miss_fin),
                 "（已加 --fill-finished-at，将用归档文件 mtime 兜底）"
                 if (miss_fin and a.fill_finished_at) else
                 ("（成功统计会漏掉它们；需要时加 --fill-finished-at）" if miss_fin else "")))

        if a.csv:
            with io.open(a.csv, "w", encoding="utf-8-sig", newline="") as f:
                f.write("id,action,evidence,file_name,title,last_error,finished_at,archived_name,object_id,object_id2\n")
                for act, lst in (("recover", recover), ("keep", keep)):
                    for t, tags, fp in lst:
                        f.write(",".join('"%s"' % str(x).replace('"', '""') for x in (
                            t.get("id"), act, "+".join(tags) or "-", t.get("file_name"),
                            t.get("title"), t.get("last_error"), t.get("finished_at"),
                            t.get("archived_name"), t.get("object_id"), fp)) + "\n")
            print("\n明细已导出: %s" % a.csv)

        if not recover:
            print("\n无需修改。")
            return 0

        ids = [t["id"] for t, tags, fp in recover]
        if not a.apply:
            print("\n=== DRY-RUN：以上 %d 条**未写入**。确认无误后加 --apply 执行。===" % len(ids))
            return 0

        # ---- 备份 ----
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(os.path.dirname(db), "backup-rev40-" + ts)
        os.makedirs(bdir, exist_ok=True)
        bpath = os.path.join(bdir, os.path.basename(db))
        dst = sqlite3.connect(bpath)
        try:
            c.backup(dst)
        finally:
            dst.close()
        print("\n已备份整库 -> %s (%d B)" % (bpath, os.path.getsize(bpath)))

        # ---- 写库 ----
        mtime_of = {t["id"]: fp for t, tags, fp in recover if fp}
        now = datetime.now().isoformat()
        fixed_err = fixed_fin = 0
        try:
            c.execute("BEGIN")
            for t in recover:
                t = t[0]
                sets, args = ["status='success'"], []
                # 旧 bug 塞进去的假提示清掉；其它错误信息保留
                if norm(t.get("last_error")) == BUG_HINT:
                    sets.append("last_error=''")
                    fixed_err += 1
                if a.fill_finished_at and not norm(t.get("finished_at")):
                    fp = mtime_of.get(t["id"]) or ""
                    when = now
                    if fp:
                        try:
                            when = datetime.fromtimestamp(os.path.getmtime(fp)).isoformat()
                        except Exception:
                            pass
                    sets.append("finished_at=?")
                    args.append(when)
                    fixed_fin += 1
                args.append(t["id"])
                c.execute("UPDATE publish_tasks SET %s WHERE id=? AND status='need_review'"
                          % ",".join(sets), args)
            c.commit()
        except Exception:
            c.rollback()
            raise

        after = status_dist(c)
        print("\n已恢复 %d 条 -> success（清掉假提示 %d 条，补 finished_at %d 条）"
              % (len(ids), fixed_err, fixed_fin))
        print("修复后状态分布:")
        for k, v in after.items():
            print("   %-14s %d" % (k, v))
        left = after.get("need_review", 0)
        print("\n仍待人工核对: %d 条（这些没有「已发出」证据，按保守原则未动）" % left)
        print("如需回滚：把 %s 覆盖回 %s 即可（注意先停服务）。" % (bpath, db))
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
