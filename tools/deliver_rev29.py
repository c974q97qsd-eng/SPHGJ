# -*- coding: utf-8 -*-
"""rev29 投递: 3 个后端文件 + frontend/dist 全量 -> 母本 \\Sphgj\sphgj。

rev29 内容:
- 后端: 登录态落盘修复(不强杀 + 等 flush + storage_state 双保险) + reap min_age 保护
- 前端: 二维码只显示裁剪后的图,去掉下方说明块与边框

投递前做基线校验(母本磁盘版 vs 本地 HEAD),避免覆盖掉别人在母本上的改动。
"""
import hashlib
import os
import subprocess
import sys

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
sys.path.insert(0, os.path.join(ROOT, "tools"))
from unc_atomic import put_file, md5_of  # noqa: E402

FILES = ["backend/browser.py", "backend/login_capture.py",
         "backend/account_manager.py",
         "tools/patch_rev29.py"]


def git_show(path):
    try:
        p = subprocess.run(["git", "show", "HEAD:" + path.replace("\\", "/")],
                           cwd=ROOT, capture_output=True, check=True)
        return hashlib.md5(p.stdout).hexdigest()
    except Exception as e:
        return f"<no-head:{e}>"


def collect_dist():
    base = os.path.join(ROOT, "frontend", "dist")
    out = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, base).replace("\\", "/")
            out.append((p, "frontend/dist/" + rel))
    return sorted(out)


def main():
    print("== 基线校验(母本 vs 本地 HEAD)==")
    bad = 0
    for f in FILES:
        dst = os.path.join(DST, f)
        if not os.path.exists(dst):
            print(f"  {f:34s} 母本不存在(将新建)")
            continue
        m_dst = md5_of(dst)
        m_head = git_show(f)
        ok = m_dst == m_head
        if not ok:
            bad += 1
        print(f"  {f:34s} {'一致' if ok else '不一致(母本=%s HEAD=%s)' % (m_dst[:8], str(m_head)[:8])}")
    if bad and "--force" not in sys.argv:
        print(f"\n[中止] {bad} 个文件母本与 HEAD 不一致,先确认再投递(加 --force 跳过)")
        return 2

    dist = collect_dist()
    print(f"\n== 投递后端 {len(FILES)} 个 ==")
    ok = 0
    for f in FILES:
        src = os.path.join(ROOT, f)
        dst = os.path.join(DST, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = put_file(src, dst)
        print(f"  {f:34s} {r}")
        if r in ("ok", "skip"):
            ok += 1

    print(f"\n== 投递前端 dist {len(dist)} 个 ==")
    dok = 0
    for src, rel in dist:
        dst = os.path.join(DST, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            r = put_file(src, dst)
        except Exception as e:
            r = "ERR:%s" % e
        if r in ("ok", "skip"):
            dok += 1
        else:
            print(f"  {rel:44s} {r}")

    print(f"\n后端 {ok}/{len(FILES)}, 前端 {dok}/{len(dist)}")
    return 0 if (ok == len(FILES) and dok == len(dist)) else 1


if __name__ == "__main__":
    sys.exit(main())
