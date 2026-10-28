# -*- coding: utf-8 -*-
"""rev40 一次性数据修复 CLI（诊断 + 手动执行）。

真正的判定与修改逻辑在 `backend/publish_fixups.py` —— 启动时自动执行的也是它，
本脚本只是把它包一层命令行，保证两边口径永远一致（不存两份实现）。

默认 dry-run：只打印方案，一条都不改。
--apply 前由 fix_mislabeled_need_review() 内部写审计文件
（data/rev40_fix_audit.json，含每条的原值，可精确回滚）。

用法
----
  python tools/fix_rev40_need_review.py                     # dry-run，看清单
  python tools/fix_rev40_need_review.py --csv detail.csv    # 顺带导出明细
  python tools/fix_rev40_need_review.py --apply             # 真改
  python tools/fix_rev40_need_review.py --db X --inbox Y    # 指定库与 inbox
  python tools/fix_rev40_need_review.py --no-fill-finished-at   # 不补 finished_at

注：启动时已自动跑过一次（幂等），需要人工复核时用本脚本 dry-run 即可。
"""
import argparse
import io
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.publish_fixups import (  # noqa: E402
    BUG_HINT, build_done_index, judge, fix_mislabeled_need_review, _norm,
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "sphgj.db"))
    ap.add_argument("--inbox", default=os.path.join(ROOT, "data", "publish_inbox"))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只 dry-run）")
    ap.add_argument("--csv", default="", help="把明细导出到该 CSV")
    ap.add_argument("--fill-finished-at", dest="fill", action="store_true", default=True,
                    help="success 但 finished_at 为空时用归档文件 mtime 兜底（默认开）")
    ap.add_argument("--no-fill-finished-at", dest="fill", action="store_false",
                    help="不补 finished_at（成功统计会漏掉这些记录）")
    ap.add_argument("--quiet", action="store_true")

    a = ap.parse_args(argv)
    db, inbox = a.db, a.inbox
    print("库文件 : %s" % db)
    print("inbox  : %s" % inbox)
    if not os.path.isfile(db):
        print("!! 库不存在，退出")
        return 2

    idx, ndirs, nfiles = build_done_index(inbox)
    print("_done 目录 %d 个，归档文件 %d 个（去重后 %d 名）" % (ndirs, nfiles, len(idx)))

    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='publish_tasks'").fetchone():
            print("!! 库里没有 publish_tasks 表，退出")
            return 2
        dist = dict(c.execute("SELECT status,COUNT(*) FROM publish_tasks "
                              "GROUP BY status ORDER BY 2 DESC").fetchall())
        print("\n当前状态分布（共 %d 条）:" % sum(dist.values()))
        for k, v in dist.items():
            print("   %-14s %d" % (k, v))

        rows = [dict(r) for r in
                c.execute("SELECT * FROM publish_tasks WHERE status='need_review' ORDER BY id")]
        print("\nneed_review 共 %d 条" % len(rows))
        if not rows:
            print("→ 没有需要修复的记录（可能是全新库，或启动时已自动修过）。无事可做。")
            return 0

        recover, keep = [], []
        for t in rows:
            ok, tags, fp = judge(t, idx)
            (recover if ok else keep).append((t, tags, fp))

        from collections import Counter
        tagc = Counter("+".join(sorted(tags)) for _t, tags, _f in recover)
        print("\n可判定为「其实已发布成功」: %d 条" % len(recover))
        for k, v in tagc.most_common():
            print("   %-44s %d" % (k, v))
        print("证据不足、保留 need_review 待人工核对: %d 条" % len(keep))

        if keep and not a.quiet:
            print("\n保留明细（前 15 条）:")
            for t, _tags, _f in keep[:15]:
                print("   id=%-5s %-40s step=%-12s err=%s"
                      % (t.get("id"), (t.get("file_name") or "")[:40],
                         _norm(t.get("step"))[:12], _norm(t.get("last_error"))[:40]))
            if len(keep) > 15:
                print("   … 其余 %d 条见 --csv" % (len(keep) - 15))

        miss = [t for t, _t2, _f in recover if not _norm(t.get("finished_at"))]
        if miss:
            print("\n恢复后 finished_at 会为空的: %d 条%s"
                  % (len(miss), "（不加 --no-fill-finished-at，会用归档文件 mtime 兜底）"
                     if a.fill else "（已指定不补，成功统计会漏掉它们）"))

        if a.csv:
            with io.open(a.csv, "w", encoding="utf-8-sig", newline="") as f:
                f.write("id,action,evidence,file_name,title,step,last_error,"
                        "finished_at,archived_name,object_id,archived_file\n")
                for act, lst in (("recover", recover), ("keep", keep)):
                    for t, tags, fp in lst:
                        f.write(",".join('"%s"' % str(x).replace('"', '""') for x in (
                            t.get("id"), act, "+".join(tags) or "-", t.get("file_name"),
                            t.get("title"), t.get("step"), t.get("last_error"),
                            t.get("finished_at"), t.get("archived_name"),
                            t.get("object_id"), fp)) + "\n")
            print("\n明细已导出: %s" % a.csv)

        if not recover:
            print("\n无需修改。")
            return 0

        if not a.apply:
            print("\n=== DRY-RUN：以上 %d 条**未写入**。确认无误后加 --apply 执行。===" % len(recover))
            return 0
    finally:
        c.close()

    print("\n=== 执行修复 ===")
    # 人工执行时先整库备份（启动时的自动修复不备份，只写审计文件）
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(os.path.dirname(os.path.abspath(db)), "backup-rev40-" + ts)
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, os.path.basename(db))
    _src = sqlite3.connect(db, timeout=30)
    _dst = sqlite3.connect(bpath)
    try:
        _src.backup(_dst)
    finally:
        _dst.close()
        _src.close()
    print("已备份整库 -> %s (%d B)" % (bpath, os.path.getsize(bpath)))
    print("如需回滚：停服务后把该文件覆盖回 %s" % db)

    res = fix_mislabeled_need_review(db, inbox, logger=None, dry_run=False,
                                     fill_finished_at=a.fill)
    if not res.get("ok"):
        print("!! 修复失败:", res.get("error"))
        return 1
    print("已恢复 %d 条 -> success（清掉假提示 %d 条，补 finished_at %d 条）"
          % (res["recover"], res["cleared_err"], res["filled"]))
    print("仍待人工核对: %d 条（没有「已发出」证据，按保守原则未动）" % res["kept"])
    print("审计文件: %s" % os.path.join(os.path.dirname(db) or ".", "rev40_fix_audit.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
