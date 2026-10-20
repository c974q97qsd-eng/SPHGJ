# -*- coding: utf-8 -*-
"""rev34 投递到母本 \\\\Sphgj\\sphgj

投递内容(全部走 unc_atomic.put_file 的两步纯 rename 通道):
  一键安装环境.bat      新增
  一键更新.bat          新增
  启动-SPHGJ.bat        改(自探测 Python)
  README.md             改
  .gitignore            改(放开 frontend/dist)
  tools/gen_rev34_bats.py  新增(生成器)
  frontend/dist/**      3 个文件(本轮已入库,母本内容应与本地一致)

基线校验:逐个比对母本磁盘版与 `git show HEAD~1:<file>` 的 md5
(rev34 之前的版本),不一致就中止 —— 防止母本被悄悄回退成旧版还照覆盖。
"""
import hashlib
import os
import subprocess
import sys

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV_REF = "HEAD~1"          # rev33 = c1b8c10
sys.path.insert(0, os.path.join(ROOT, "tools"))
from unc_atomic import put_file, md5_of  # noqa: E402

FILES = [
    "一键安装环境.bat",
    "一键更新.bat",
    "启动-SPHGJ.bat",
    "README.md",
    ".gitignore",
    "tools/gen_rev34_bats.py",
]


def norm_md5(data):
    """把 CRLF 归一成 LF 再算 md5。

    仓库里存的是 LF(core.autocrlf=true),母本磁盘是 CRLF,直接比 md5 会假报
    "不一致" —— 2026-10-21 就在这里踩过一次,差点以为母本被回退。
    """
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_show_raw(path, ref=PREV_REF):
    try:
        p = subprocess.run(["git", "show", "%s:%s" % (ref, path.replace("\\", "/"))],
                           cwd=ROOT, capture_output=True, check=True)
        return p.stdout
    except Exception:
        return None


def collect_dist():
    base = os.path.join(ROOT, "frontend", "dist")
    out = []
    for r, _d, files in os.walk(base):
        for fn in files:
            p = os.path.join(r, fn)
            rel = "frontend/dist/" + os.path.relpath(p, base).replace("\\", "/")
            out.append((p, rel))
    return sorted(out)


def main():
    force = "--force" in sys.argv
    bad = 0
    print("== 基线校验(母本磁盘 vs %s) ==" % PREV_REF)
    for f in FILES:
        dst = os.path.join(DST, f)
        if not os.path.exists(dst):
            print("  %-26s 母本不存在 -> 将新建" % f)
            continue
        cur = open(dst, "rb").read()
        old = git_show_raw(f)
        if old is None:
            print("  %-26s 上一版无此文件(新增文件) -> 跳过基线" % f)
            continue
        same_raw = hashlib.md5(cur).hexdigest() == hashlib.md5(old).hexdigest()
        same_norm = norm_md5(cur) == norm_md5(old)
        if same_raw:
            print("  %-26s 一致" % f)
        elif same_norm:
            print("  %-26s 一致(仅换行符差异 CRLF/LF)" % f)
        else:
            bad += 1
            print("  %-26s !! 内容不同 母本 %d 字节 / 上一版 %d 字节" % (f, len(cur), len(old)))
    if bad and not force:
        print("\n[中止] %d 个基线不一致,检查后再跑(或加 --force)" % bad)
        return 2

    print("\n== 投递 项目根 ===")
    for f in FILES:
        r = put_file(os.path.join(ROOT, f), os.path.join(DST, f))
        print("  %-26s %s" % (f, r))

    print("\n== 投递 frontend/dist ==")
    for src, rel in collect_dist():
        dst = os.path.join(DST, rel.replace("/", os.sep))
        same = os.path.exists(dst) and md5_of(dst) == md5_of(src)
        r = put_file(src, dst)
        print("  %-40s %s%s" % (rel, r, " (内容相同)" if same else ""))

    print("\n== 投递后校验 ==")
    bad2 = 0
    for f in FILES + [rel for _s, rel in collect_dist()]:
        src = os.path.join(ROOT, f.replace("/", os.sep))
        dst = os.path.join(DST, f.replace("/", os.sep))
        m1 = md5_of(src)
        m2 = md5_of(dst) if os.path.exists(dst) else "<缺失>"
        if m1 != m2:
            bad2 += 1
            print("  !! %-40s 本地 %s != 母本 %s" % (f, m1, m2))
    print("  校验完成:%d 个文件, 不一致 %d 个" % (len(FILES) + len(collect_dist()), bad2))
    return 0 if bad2 == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
