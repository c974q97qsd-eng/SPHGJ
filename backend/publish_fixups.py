# -*- coding: utf-8 -*-
"""一次性数据修正：publish_tasks 历史误标（need_review）。

rev40 背景
----------
旧版 `Storage.reset_running_tasks()` 的 UPDATE 漏了 WHERE：只要启动时库里存在
1 条 running（重启、强杀都会留下），全表（含已 success / failed 的历史记录）
统统被刷成 need_review。`need_review` 在代码里**只有那一个写入点**，
所以库里的 need_review 全是它刷出来的。

代码已修（现在只更新 running 的 id），但历史脏数据要单独回滚 —— 本模块负责它，
在启动时自动执行，随代码更新一起到达每台用户机，不需要人工敲命令。

修复口径（保守）
----------------
只回滚**有硬证据证明曾发布成功**的记录，证据任一命中：
  A. archived_name 非空        （发布成功归档后才回填）
  B. object_id 非空            （发布成功后回传的作品 id）
  C. step == 'done'            （成功路径唯一写 done）
  D. publish_inbox 下 _done/ 里存在对应归档文件
     （兼容 inbox/_done 与 inbox/<店名>/_done 两种结构；
       按 archived_name / file_name 精确匹配，或以 "-<file_name>" 结尾匹配，
       兼容 rename_after_publish 的「日期发布-标题-原名」命名）
无证据的一律保留 need_review 等人工核对 —— 把真没发出去的记录标成成功，
比误标更难挽回。

安全约束
--------
* 幂等：已恢复的记录不再是 need_review，重复启动不会重复改。
* need_review 为 0 时**不扫 _done 目录**（零开销），直接返回。
* 只改 status / 清掉 bug 写入的假提示 / 可选补 finished_at，绝不删行。
* 每次改动写审计文件 `data/rev40_fix_audit.json`（含原值，可精确回滚）。
* 环境变量 `SPHGJ_NO_AUTOFIX=1` 可总体禁用自动修正。
* 本模块绝不抛异常给调用方（启动流程不能因为修数据失败而中断）。
"""
import json
import os
import sqlite3
from datetime import datetime

# 旧 bug 写进去的假提示（恢复成功时要清掉；其它 last_error 一律保留）
BUG_HINT = "进程中断,不确定是否已发出,请到视频号后台核对后再决定重试"

AUDIT_NAME = "rev40_fix_audit.json"


def _norm(s):
    return (s or "").strip()


def build_done_index(inbox):
    """扫描 inbox 下所有 _done/ 目录，返回 ({basename: [path, ...]}, 目录数, 文件数)。"""
    idx = {}
    ndirs = nfiles = 0
    if not inbox or not os.path.isdir(inbox):
        return idx, 0, 0
    for cur, _subs, files in os.walk(inbox):
        if os.path.basename(cur) != "_done":
            continue
        ndirs += 1
        for fn in files:
            nfiles += 1
            idx.setdefault(fn, []).append(os.path.join(cur, fn))
    return idx, ndirs, nfiles


def match_done_file(task, idx):
    """返回命中的归档文件路径（无命中返回 ""）。"""
    an = _norm(task.get("archived_name"))
    fn = _norm(task.get("file_name"))
    if an:
        hit = idx.get(an)
        if hit:
            return hit[0]
    if fn:
        hit = idx.get(fn)
        if hit:
            return hit[0]
        for name, paths in idx.items():   # 「日期发布-标题-原名」
            if name.endswith("-" + fn) or name.endswith(fn):
                return paths[0]
    return ""


def judge(task, idx):
    """返回 (是否可恢复, 证据标签列表, 命中的归档文件路径)。"""
    tags = []
    if _norm(task.get("archived_name")):
        tags.append("archived_name")
    if _norm(task.get("object_id")):
        tags.append("object_id")
    if _norm(task.get("step")).lower() == "done":
        tags.append("step_done")
    fp = match_done_file(task, idx)
    if fp:
        tags.append("done_file")
    return bool(tags), tags, fp


def _write_audit(path, payload):
    try:
        old = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                old = raw if isinstance(raw, list) else [raw]
            except Exception:
                old = []
        old.append(payload)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def fix_mislabeled_need_review(db_path, inbox=None, logger=None, dry_run=False,
                               fill_finished_at=True, audit_dir=None):
    """把被 bug 误标成 need_review 的「其实已发布」记录恢复为 success。

    db_path      : SQLite 库路径
    inbox        : publish_inbox 目录（None 则跳过 D 类文件证据）
    dry_run      : True 只计算不写库
    fill_finished_at : success 但 finished_at 为空时用归档文件 mtime 兜底
    audit_dir    : 审计文件目录，默认库所在目录

    返回 dict:
      {"ok","skipped","had","recover","kept","cleared_err","filled",
       "ids","detail","error"}
    """
    out = {"ok": True, "skipped": False, "had": 0, "recover": 0, "kept": 0,
           "cleared_err": 0, "filled": 0, "ids": [], "error": ""}

    def log(msg, level="warning"):
        if logger is not None:
            try:
                getattr(logger, level)(msg)
                return
            except Exception:
                pass

    if os.environ.get("SPHGJ_NO_AUTOFIX") == "1":
        out["skipped"] = True
        out["error"] = "disabled by SPHGJ_NO_AUTOFIX"
        return out

    if not db_path or not os.path.isfile(db_path):
        out["skipped"] = True
        out["error"] = "db not found"
        return out

    c = None
    try:
        c = sqlite3.connect(db_path, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        c.row_factory = sqlite3.Row

        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='publish_tasks'").fetchone():
            out["skipped"] = True
            out["error"] = "no publish_tasks"
            return out

        rows = [dict(r) for r in
                c.execute("SELECT * FROM publish_tasks WHERE status='need_review'").fetchall()]
        out["had"] = len(rows)
        if not rows:
            out["skipped"] = True        # 常态：没事可做，连目录都不扫
            return out

        idx, ndirs, nfiles = build_done_index(inbox)
        recover, keep = [], []
        for t in rows:
            ok, tags, fp = judge(t, idx)
            (recover if ok else keep).append((t, tags, fp))
        out["recover"] = len(recover)
        out["kept"] = len(keep)
        out["ids"] = [t["id"] for t, _g, _f in recover]

        log("[publish] rev40 一次性修正：need_review %d 条，其中 %d 条有「已发布」证据"
            "可恢复，%d 条保留人工核对（_done 目录 %d 个 / 归档文件 %d 个）"
            % (len(rows), len(recover), len(keep), ndirs, nfiles))

        if keep:
            log("[publish] rev40 保留 need_review 的 id（前 10）: %s"
                % ",".join(str(t["id"]) for t, _g, _f in keep[:10]))

        if not recover:
            return out

        # 摘要（审计用：原值全留，可精确回滚）
        detail = [{
            "id": t["id"], "evidence": tags, "archived_file": fp,
            "before": {"status": t.get("status"), "step": t.get("step"),
                       "last_error": t.get("last_error"),
                       "finished_at": t.get("finished_at"),
                       "archived_name": t.get("archived_name"),
                       "object_id": t.get("object_id")},
        } for t, tags, fp in recover]

        adir = audit_dir or os.path.dirname(db_path) or "."
        apath = os.path.join(adir, AUDIT_NAME)
        stamp = datetime.now().isoformat()
        payload = {"ts": stamp, "dry_run": bool(dry_run), "applied": False,
                   "had": out["had"], "recover": out["recover"], "kept": out["kept"],
                   "detail": detail}
        if not dry_run:
            _write_audit(apath, payload)

        if dry_run:
            out["ok"] = True
            return out

        mtime_of = {t["id"]: fp for t, _g, fp in recover if fp}
        now = datetime.now().isoformat()
        try:
            c.execute("BEGIN")
            for t, _tags, _fp in recover:
                sets, args = ["status='success'"], []
                if _norm(t.get("last_error")) == BUG_HINT:
                    sets.append("last_error=''")
                    out["cleared_err"] += 1
                if fill_finished_at and not _norm(t.get("finished_at")):
                    fp = mtime_of.get(t["id"]) or ""
                    when = now
                    if fp:
                        try:
                            when = datetime.fromtimestamp(os.path.getmtime(fp)).isoformat()
                        except Exception:
                            pass
                    sets.append("finished_at=?")
                    args.append(when)
                    out["filled"] += 1
                args.append(t["id"])
                # AND status='need_review' 防并发：只改仍是 need_review 的那条
                c.execute("UPDATE publish_tasks SET %s WHERE id=? AND status='need_review'"
                          % ",".join(sets), args)
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise

        payload["applied"] = True
        payload["filled_finished_at"] = out["filled"]
        payload["cleared_bug_hint"] = out["cleared_err"]
        _write_audit(apath, payload)

        log("[publish] rev40 一次性修正完成：%d 条 need_review -> success"
            "（清掉假提示 %d 条，补 finished_at %d 条；审计 %s）"
            % (out["recover"], out["cleared_err"], out["filled"], apath))
        return out

    except Exception as e:
        out["ok"] = False
        out["error"] = "%s: %s" % (type(e).__name__, e)
        log("[publish] rev40 一次性修正失败（不影响启动）: %s" % out["error"])
        return out
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
