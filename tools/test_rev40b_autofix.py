# -*- coding: utf-8 -*-
"""rev40 启动自动修正单测：backend.publish_fixups.fix_mislabeled_need_review

这是「用户机更新完就自动生效」的那段代码，必须证明：
  1. 能把有硬证据的 need_review 恢复成 success
  2. 没有证据的一条都不动（把没发出去的标成成功，比误标更糟）
  3. 幂等：第二次启动不再改动
  4. need_review 为 0 时不扫目录、不写审计（常态化零开销）
  5. SPHGJ_NO_AUTOFIX=1 能整体禁用
  6. 审计文件写下了每条的原值（可精确回滚）
  7. 任何异常都不外抛（启动流程不能因此中断）
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.storage import Storage                       # noqa: E402
from backend import publish_fixups as FX                  # noqa: E402

PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, extra)


class Cap(object):
    """假 logger，收集日志行。"""
    def __init__(self):
        self.lines = []

    def _add(self, m):
        self.lines.append(m)

    warning = info = error = debug = _add

    def text(self):
        return "\n".join(self.lines)


def make_db(tmp, inbox, n_ok_file=200, n_ok_shop=59, n_failed=25, n_pending=20):
    """造与现场同构的脏库，返回 (db_path, ids)。"""
    db = os.path.join(tmp, "sphgj.db")
    d_root = os.path.join(inbox, "_done")
    d_shop = os.path.join(inbox, "免税仓", "_done")
    for d in (d_root, d_shop):
        os.makedirs(d, exist_ok=True)
    st = Storage(db)
    F = "finderA"
    ids = {"ok_file": [], "ok_shop": [], "failed": [], "pending": []}

    def mk(tag):
        src = os.path.join(tmp, "src", tag + ".mp4")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        open(src, "wb").write(b"x")
        tid, created = st.enqueue_task(F, src, tag + ".mp4", 1, "fp-" + tag, "标题" + tag, 0)
        assert created
        return tid

    for i in range(n_ok_file):
        ids["ok_file"].append(mk("okf%03d" % i))
    for i in range(n_ok_shop):
        ids["ok_shop"].append(mk("oks%03d" % i))
    for i in range(n_failed):
        ids["failed"].append(mk("bad%03d" % i))
    for i in range(n_pending):
        ids["pending"].append(mk("pen%03d" % i))

    c = sqlite3.connect(db)
    # A/B/C/D 四类证据齐全
    for tid in ids["ok_file"]:
        name = "2026-10-20发布-标题-okf.mp4"
        open(os.path.join(d_root, "okf-%d.mp4" % tid), "wb").write(b"x")
        c.execute("""UPDATE publish_tasks SET status='need_review', step='done',
                     object_id=?, finished_at=?, archived_name=?, last_error=?
                     WHERE id=?""",
                  ("obj-%d" % tid, "2026-10-20T03:56:06", name, FX.BUG_HINT, tid))
    # 只有 D 类证据（归档文件在店铺子目录，且名字靠 file_name 后缀匹配）
    for tid in ids["ok_shop"]:
        base = c.execute("SELECT file_name FROM publish_tasks WHERE id=?", (tid,)).fetchone()[0]
        open(os.path.join(d_shop, "2026-10-21发布-标题-%s" % base), "wb").write(b"x")
        c.execute("""UPDATE publish_tasks SET status='need_review', step='upload',
                     object_id='', finished_at=NULL, archived_name='', last_error=?
                     WHERE id=?""", (FX.BUG_HINT, tid))
    for tid in ids["failed"]:
        c.execute("""UPDATE publish_tasks SET status='need_review', step='exception',
                     last_error='RuntimeError: 上传超时' WHERE id=?""", (tid,))
    for tid in ids["pending"]:
        c.execute("""UPDATE publish_tasks SET status='need_review', step='prepare',
                     last_error='' WHERE id=?""", (tid,))
    c.commit()
    c.close()
    return db, ids


def dist(db):
    c = sqlite3.connect(db)
    d = dict(c.execute("SELECT status,COUNT(*) FROM publish_tasks GROUP BY status").fetchall())
    c.close()
    return d


def main():
    tmp = tempfile.mkdtemp(prefix="rev40auto_")
    inbox = os.path.join(tmp, "publish_inbox")
    db, ids = make_db(tmp, inbox)
    cap = Cap()

    print("\n[0] 现场形状")
    ck(dist(db).get("need_review") == 304, "304 条 need_review", str(dist(db)))
    ck(len(ids["ok_file"]) + len(ids["ok_shop"]) == 259, "其中 259 条其实已发布")

    print("\n[1] 运行前：need_review=0 的库不扫目录、不写审计")
    tmp0 = tempfile.mkdtemp(prefix="rev40clean_")
    db0 = os.path.join(tmp0, "clean.db")
    st0 = Storage(db0)
    st0.enqueue_task("f", os.path.join(tmp0, "a.mp4"), "a.mp4", 1, "fp-a", "t", 0)
    r0 = FX.fix_mislabeled_need_review(db0, os.path.join(tmp0, "no_such_inbox"))
    ck(r0["ok"] and r0["skipped"] and r0["had"] == 0, "干净库直接跳过", str(r0))
    ck(not os.path.exists(os.path.join(tmp0, FX.AUDIT_NAME)), "干净库不写审计文件")

    print("\n[2] 自动修正")
    r = FX.fix_mislabeled_need_review(db, inbox, logger=cap)
    ck(r["ok"], "返回 ok", r.get("error"))
    ck(r["had"] == 304, "扫到 304 条 need_review", str(r["had"]))
    ck(r["recover"] == 259, "★ 恢复 259 条（200 四证据 + 59 仅归档文件）", str(r["recover"]))
    ck(r["kept"] == 45, "★ 保留 45 条无证据的", str(r["kept"]))
    d = dist(db)
    ck(d.get("success") == 259 and d.get("need_review") == 45, "库状态正确", str(d))

    c = sqlite3.connect(db)
    # 无证据的一条都不许动
    kept_ok = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='need_review'
                           AND (last_error='RuntimeError: 上传超时' OR step='prepare')""").fetchone()[0]
    ck(kept_ok == 45, "★ 保留记录的 last_error / step 原样（未被改写）", str(kept_ok))
    # 假提示必须清掉，别的错误信息不许清
    hint = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='success'
                        AND last_error=?""", (FX.BUG_HINT,)).fetchone()[0]
    ck(hint == 0, "★ 恢复的记录不再带 bug 假提示", str(hint))
    other = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='success'
                         AND last_error<>''""").fetchone()[0]
    ck(other == 0, "没有被误清的真实错误（原本都是假提示）", str(other))
    # finished_at 兜底
    nullfin = c.execute("""SELECT COUNT(*) FROM publish_tasks WHERE status='success'
                           AND (finished_at IS NULL OR finished_at='')""").fetchone()[0]
    ck(nullfin == 0, "★ finished_at 全部有值（用归档文件 mtime 兜底）", str(nullfin))
    shop_ids = ids["ok_shop"]
    q = ",".join("?" * len(shop_ids))
    okf = c.execute("SELECT finished_at FROM publish_tasks WHERE id IN (%s) LIMIT 1" % q,
                    shop_ids).fetchone()[0]
    ck(str(okf).startswith("2026-10-2"), "兜底值来自归档文件 mtime（不是 now）", str(okf))
    c.close()

    print("\n[3] 审计文件")
    ap = os.path.join(tmp, FX.AUDIT_NAME)
    ck(os.path.isfile(ap), "审计文件已生成")
    with open(ap, encoding="utf-8") as f:
        au = json.load(f)
    last = au[-1]
    ck(last.get("applied") is True, "applied=True")
    ck(last.get("recover") == 259, "审计记录恢复条数", str(last.get("recover")))
    ck(len(last.get("detail") or []) == 259, "审计含 259 条明细")
    one = (last.get("detail") or [{}])[0]
    ck(one.get("before", {}).get("status") == "need_review",
       "★ 明细保留了改前状态（可回滚）", str(one.get("before")))
    ck("evidence" in one and one["evidence"], "明细记录了判定证据", str(one.get("evidence")))

    print("\n[4] 幂等：第二次启动")
    before = dist(db)
    r2 = FX.fix_mislabeled_need_review(db, inbox, logger=Cap())
    ck(r2["ok"] and r2["had"] == 45, "第二次只剩 45 条 need_review", str(r2.get("had")))
    ck(r2["recover"] == 0, "★ 第二次一条都不恢复", str(r2.get("recover")))
    ck(dist(db) == before, "★ 状态分布不变", "%s vs %s" % (dist(db), before))

    print("\n[5] 环境变量禁用")
    tmp2 = tempfile.mkdtemp(prefix="rev40dis_")
    inbox2 = os.path.join(tmp2, "publish_inbox")
    db2, _ids2 = make_db(tmp2, inbox2, n_ok_file=3, n_ok_shop=0, n_failed=1, n_pending=0)
    os.environ["SPHGJ_NO_AUTOFIX"] = "1"
    try:
        r3 = FX.fix_mislabeled_need_review(db2, inbox2, logger=Cap())
    finally:
        os.environ.pop("SPHGJ_NO_AUTOFIX", None)
    ck(r3["skipped"] and "SPHGJ_NO_AUTOFIX" in (r3.get("error") or ""), "被环境变量禁用", str(r3))
    ck(dist(db2).get("need_review") == 4, "禁用后一条都没改", str(dist(db2)))

    print("\n[6] 异常不外抛")
    r4 = FX.fix_mislabeled_need_review(os.path.join(tmp, "nope.db"), inbox, logger=Cap())
    ck(r4["ok"] is False or r4["skipped"], "库不存在时返回而不抛", str(r4))
    bad = os.path.join(tmp, "bad.db")
    open(bad, "wb").write(b"not a sqlite file, just garbage" * 10)
    r5 = FX.fix_mislabeled_need_review(bad, inbox, logger=Cap())
    ck(r5["ok"] is False and r5["error"], "坏库文件返回 error 而不抛", str(r5))

    print("\n[7] 归档目录两种结构都认")
    tmp3 = tempfile.mkdtemp(prefix="rev40st_")
    inbox3 = os.path.join(tmp3, "publish_inbox")
    db3, ids3 = make_db(tmp3, inbox3, n_ok_file=0, n_ok_shop=4, n_failed=0, n_pending=0)
    r6 = FX.fix_mislabeled_need_review(db3, inbox3, logger=Cap())
    ck(r6["recover"] == 4, "★ 只在 inbox/<店名>/_done/ 有文件也能恢复", str(r6))

    print("\n合计 PASS=%d FAIL=%d" % (PASS, FAIL))
    for d in (tmp, tmp0, tmp2, tmp3):
        shutil.rmtree(d, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
