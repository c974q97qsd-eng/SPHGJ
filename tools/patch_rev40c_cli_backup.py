# -*- coding: utf-8 -*-
"""rev40 CLI 补丁：--apply 保留整库备份 + 兼容 --fill-finished-at 写法。

启动时的自动修复不备份整库（每台机器每次启动都拷 95MB 不可接受，改由审计文件
记录原值）；但 CLI 是人工执行，保留 sqlite backup 更稳。
"""
import io
import os
import sys

P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "tools", "fix_rev40_need_review.py")


def main():
    s = io.open(P, encoding="utf-8").read()
    if "backup-rev40-" in s:
        print("已应用过，跳过。")
        return 0

    # 1) 参数：默认开启，两种写法都认
    old1 = '''    ap.add_argument("--no-fill-finished-at", action="store_true",
                    help="success 但 finished_at 为空时不补（默认用归档文件 mtime 兜底）")'''
    new1 = '''    ap.add_argument("--fill-finished-at", dest="fill", action="store_true", default=True,
                    help="success 但 finished_at 为空时用归档文件 mtime 兜底（默认开）")
    ap.add_argument("--no-fill-finished-at", dest="fill", action="store_false",
                    help="不补 finished_at（成功统计会漏掉这些记录）")'''
    assert s.count(old1) == 1
    s = s.replace(old1, new1, 1)

    # 2) 使用处
    assert s.count("a.no_fill_finished_at") == 2, s.count("a.no_fill_finished_at")
    s = s.replace("if not a.no_fill_finished_at else", "if a.fill else")
    s = s.replace("fill_finished_at=not a.no_fill_finished_at", "fill_finished_at=a.fill")

    # 3) --apply 分支：先整库备份
    old3 = '''    print("\\n=== 执行修复 ===")
    res = fix_mislabeled_need_review(db, inbox, logger=None, dry_run=False,
                                     fill_finished_at=a.fill)'''
    new3 = '''    print("\\n=== 执行修复 ===")
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
                                     fill_finished_at=a.fill)'''
    assert s.count(old3) == 1, s.count(old3)
    s = s.replace(old3, new3, 1)

    io.open(P, "w", encoding="utf-8", newline="").write(s)

    t = io.open(P, encoding="utf-8").read()
    checks = [
        ("备份逻辑已加", "backup-rev40-" in t),
        ("--fill-finished-at 兼容", '"--fill-finished-at", dest="fill"' in t),
        ("旧参数引用已清除", "a.no_fill_finished_at" not in t),
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
