# -*- coding: utf-8 -*-
"""rev41 回归单测：发布归档跟着店铺目录走（inbox/<店名>/_done/）。

覆盖的正是旧行为的错误面 + 新行为的边界：
  * 两个店铺各自归档到自己的 _done，互不混（旧版全塞 inbox/_done）
  * src 直接在 inbox 根 -> 退回 inbox/_done（不破坏旧结构）
  * src 不在 inbox 之下 -> 退回 inbox/_done，且**不许**在源文件旁边建 _done
  * src 已在 _done 里 -> 原地短路返回，不重复搬、不报错
  * 重命名 / 同名加 (2) / 260 路径上限按实际目标目录算
  * 归档后 scan_inbox 不把 _done 当店铺目录

跑：python tools/test_rev41_archive_dir.py
"""
import io
import os
import shutil
import sys
import tempfile
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backend.publish_queue as PQ   # noqa: E402

PASS = 0
FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  << " + str(extra) if extra else ""))


def mkq(inbox, rename=False):
    """不跑 __init__ 造一个只带归档所需状态的队列实例。

    注意 `cfg` 是只读 property（由 self.config["publish"] 派生），
    不能直接赋值，只能塞 self.config。
    """
    q = PQ.PublishQueue.__new__(PQ.PublishQueue)
    q.config = {"publish": {"inbox_dir": inbox, "rename_after_publish": rename}}
    return q


def wf(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as f:
        f.write(data)
    return path


def case_shop_split(base):
    """两个店铺各归各的 _done，不再混在 inbox 根。"""
    print("\n[1] 归档跟着店铺目录走")
    inbox = os.path.join(base, "inbox")
    a = wf(os.path.join(inbox, "A店", "a.mp4"))
    b = wf(os.path.join(inbox, "B店", "b.mp4"))
    q = mkq(inbox)

    name_a = q._archive({"src_path": a, "title": "标题A"})
    name_b = q._archive({"src_path": b, "title": "标题B"})

    ck(name_a == "a.mp4", "A店 返回原名", name_a)
    ck(name_b == "b.mp4", "B店 返回原名", name_b)
    ck(os.path.isfile(os.path.join(inbox, "A店", "_done", "a.mp4")), "A店 -> A店/_done/")
    ck(os.path.isfile(os.path.join(inbox, "B店", "_done", "b.mp4")), "B店 -> B店/_done/")
    ck(not os.path.exists(a) and not os.path.exists(b), "源文件已移走")
    ck(not os.path.exists(os.path.join(inbox, "_done", "a.mp4")),
       "★ 不再塞进 inbox 根 _done")
    ck(not os.path.isdir(os.path.join(inbox, "_done")), "inbox 根 _done 根本没被创建")

    sub, in_shop = q._archive_subdir(os.path.join(inbox, "A店", "z.mp4"))
    ck(in_shop is True, "_archive_subdir 标记 in_shop=True")
    ck(os.path.normcase(sub) == os.path.normcase(os.path.join(inbox, "A店", "_done")),
       "_archive_subdir 目标目录正确", sub)


def case_fallback_root(base):
    """src 直接在 inbox 根 -> 退回 inbox/_done（保持旧行为）。"""
    print("\n[2] 直接在 inbox 根的文件退回 inbox/_done")
    inbox = os.path.join(base, "inbox2")
    p = wf(os.path.join(inbox, "root.mp4"))
    q = mkq(inbox)
    n = q._archive({"src_path": p, "title": "t"})
    ck(n == "root.mp4", "归档成功", n)
    ck(os.path.isfile(os.path.join(inbox, "_done", "root.mp4")), "落到 inbox/_done/")
    ck(not os.path.isdir(os.path.join(inbox, "root.mp4", "_done")), "没有乱建目录")


def case_outside_inbox(base):
    """src 不在 inbox 下 -> 退回 inbox/_done，且不在源文件旁建 _done。"""
    print("\n[3] inbox 之外的文件退回 inbox/_done（不乱建目录）")
    inbox = os.path.join(base, "inbox3")
    outside = os.path.join(base, "outside")
    p = wf(os.path.join(outside, "o.mp4"))
    q = mkq(inbox)
    n = q._archive({"src_path": p, "title": "t"})
    ck(n == "o.mp4", "归档成功", n)
    ck(os.path.isfile(os.path.join(inbox, "_done", "o.mp4")), "落到 inbox/_done/")
    ck(not os.path.isdir(os.path.join(outside, "_done")),
       "★ 没在源文件旁边建 _done（不能污染用户目录）")
    sub, in_shop = q._archive_subdir(p)
    ck(in_shop is False, "in_shop=False（走了退回分支）")


def case_already_archived(base):
    """src 已在 _done 里 -> 原地短路，不搬不报错。"""
    print("\n[4] 已在 _done 里 -> 原地短路")
    inbox = os.path.join(base, "inbox4")
    p = wf(os.path.join(inbox, "C店", "_done", "done.mp4"))
    q = mkq(inbox)
    n = q._archive({"src_path": p, "title": "t"})
    ck(n == "done.mp4", "返回现有文件名", n)
    ck(os.path.isfile(p), "★ 文件没被搬走")
    ck(not os.path.isdir(os.path.join(inbox, "_done")), "没多出根级 _done")
    lst = sorted(os.listdir(os.path.join(inbox, "C店", "_done")))
    ck(lst == ["done.mp4"], "目标目录内没有 (2) 副本", lst)


def case_rename_and_dup(base):
    """rename_after_publish：文件名格式 + 同名不覆盖。"""
    print("\n[5] 重命名格式与同名不覆盖")
    inbox = os.path.join(base, "inbox5")
    p1 = wf(os.path.join(inbox, "D店", "原名.mp4"))
    q = mkq(inbox, rename=True)
    task = {"src_path": p1, "title": "厚底老爹鞋 视觉增高"}
    n1 = q._archive(task)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    ck(n1 == "%s发布-厚底老爹鞋 视觉增高-原名.mp4" % today, "文件名格式正确", n1)
    ck(os.path.isfile(os.path.join(inbox, "D店", "_done", n1)), "落在 D店/_done/")

    p2 = wf(os.path.join(inbox, "D店", "原名.mp4"), b"yy")
    n2 = q._archive({"src_path": p2, "title": "厚底老爹鞋 视觉增高"})
    ck(n2.endswith("(2).mp4"), "同名加 (2) 不覆盖", n2)
    ck(os.path.isfile(os.path.join(inbox, "D店", "_done", n1)), "★ 原归档文件还在")


def case_long_path(base):
    """260 上限按实际目标目录算（多一级店名后仍不超长）。"""
    print("\n[6] 长标题 + 长店名不超 250")
    inbox = os.path.join(base, "inbox6")
    shop = "很长很长很长很长的店铺目录名称测试"
    stem = "素材" * 10
    p = wf(os.path.join(inbox, shop, stem + ".mp4"))
    q = mkq(inbox, rename=True)
    n = q._archive({"src_path": p, "title": "标题" * 90})
    dest = os.path.join(inbox, shop, "_done", n)
    ck(os.path.isfile(dest), "长标题仍归档成功", n)
    ck(len(dest) <= 250, "完整路径 <= 250（%d）" % len(dest))
    ck(stem in n, "★ 原文件名没被截断（溯源关键）", n[:60])


def case_missing_src(base):
    print("\n[7] 容错")
    inbox = os.path.join(base, "inbox7")
    q = mkq(inbox)
    ck(q._archive({"src_path": os.path.join(inbox, "X店", "nope.mp4")}) == "",
       "src 不存在 -> 返回空串")
    ck(q._archive({"src_path": ""}) == "", "src 为空 -> 返回空串")
    ck(q._archive({}) == "", "task 无 src_path -> 返回空串")


def case_trailing_sep_and_case(base):
    print("\n[8] inbox_dir 带尾分隔符 / 大小写差异")
    inbox = os.path.join(base, "inbox8")
    p = wf(os.path.join(inbox, "E店", "e.mp4"))

    q1 = mkq(inbox + os.sep + os.sep)
    sub1, in1 = q1._archive_subdir(p)
    ck(in1 is True, "尾部分隔符不影响识别", sub1)

    if os.name == "nt":
        q2 = mkq(inbox.upper())
        sub2, in2 = q2._archive_subdir(p)
        ck(in2 is True, "★ 大小写不同仍认作店铺目录（Windows 不敏感）", sub2)
        ck(os.path.normcase(sub2) == os.path.normcase(os.path.join(inbox, "E店", "_done")),
           "目标目录仍是真实路径")
    else:
        print("  SKIP 大小写用例（非 Windows）")


def case_scan_inbox_skips_done(base):
    """归档后 scan_inbox 仍只把店铺目录当店铺，_done 不算；再丢新素材仍能认领。"""
    print("\n[9] 归档后 scan_inbox 不把 _done 当店铺")
    inbox = os.path.join(base, "inbox9")
    a = wf(os.path.join(inbox, "F店", "f.mp4"))
    wf(os.path.join(inbox, "G店", "g.mp4"))
    q = mkq(inbox)
    q._archive({"src_path": a, "title": "t"})   # 造出 F店/_done/
    ck(os.path.isdir(os.path.join(inbox, "F店", "_done")), "F店/_done 已产生")
    # 归档后用户再往同一店铺丢新素材——这才是常态，必须还能被认领
    wf(os.path.join(inbox, "F店", "f2.mp4"))

    q.match_finder = lambda name: (None, "stub-unbound")
    st = q.scan_inbox()
    ck(st["dirs"] == 2, "★ 只数到 2 个店铺目录（F店/G店），_done 不算", st["dirs"])
    names = [u["dir"] for u in st["unbound"]]
    ck("_done" not in names, "unbound 里没有 _done", names)
    ck(sorted(names) == ["F店", "G店"], "两个店铺都被列出（含归档后仍可继续收素材的 F店）", names)


def case_old_behavior_reverse(base):
    """反向验证：旧实现（无条件 inbox/_done）确实会把不同店铺混在一起。

    只写「新代码通过」不够 —— 必须证明这组用例能抓到原 bug。
    """
    print("\n[10] 反向验证：旧实现会混合店铺")
    inbox = os.path.join(base, "inbox10")
    a = wf(os.path.join(inbox, "A店", "a.mp4"))
    b = wf(os.path.join(inbox, "B店", "b.mp4"))
    # 复刻旧实现：root = self.inbox_dir; sub = os.path.join(root, SUB_DONE)
    for p in (a, b):
        sub = os.path.join(inbox, PQ.SUB_DONE)
        os.makedirs(sub, exist_ok=True)
        shutil.move(p, os.path.join(sub, os.path.basename(p)))
    lst = sorted(os.listdir(os.path.join(inbox, "_done")))
    ck(lst == ["a.mp4", "b.mp4"], "旧实现把两家店的素材混进 inbox/_done（复现原问题）", lst)
    ck(not os.path.isdir(os.path.join(inbox, "A店", "_done")),
       "旧实现从不创建店铺级 _done")
    ck(not os.path.isdir(os.path.join(inbox, "B店", "_done")),
       "旧实现从不创建店铺级 _done（B店）")


def case_fixups_compatible(base):
    """rev40 的 _done 索引必须仍能扫到店铺级 _done（两处改动的交叉影响）。"""
    print("\n[11] 与 rev40 数据修复的兼容性")
    import backend.publish_fixups as FX
    inbox = os.path.join(base, "inbox11")
    p = wf(os.path.join(inbox, "H店", "h.mp4"))
    mkq(inbox)._archive({"src_path": p, "title": "t"})
    idx, ndirs, nfiles = FX.build_done_index(inbox)
    ck(ndirs == 1, "build_done_index 扫到 1 个 _done", ndirs)
    ck(nfiles == 1, "扫到 1 个归档文件", nfiles)
    hit = FX.match_done_file({"file_name": "h.mp4", "archived_name": "h.mp4"}, idx)
    ck(bool(hit), "★ 能按文件名命中店铺级 _done 里的归档", hit)
    ck(os.path.normcase(hit or "").startswith(os.path.normcase(os.path.join(inbox, "H店", "_done"))),
       "命中路径指向 <店名>/_done/", hit)


def main():
    base = tempfile.mkdtemp(prefix="rev41_")
    try:
        case_shop_split(base)
        case_fallback_root(base)
        case_outside_inbox(base)
        case_already_archived(base)
        case_rename_and_dup(base)
        case_long_path(base)
        case_missing_src(base)
        case_trailing_sep_and_case(base)
        case_scan_inbox_skips_done(base)
        case_old_behavior_reverse(base)
        case_fixups_compatible(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n合计 %d 项，PASS %d，FAIL %d" % (PASS + FAIL, PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
