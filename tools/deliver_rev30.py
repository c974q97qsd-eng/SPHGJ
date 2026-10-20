# -*- coding: utf-8 -*-
"""rev30 投递: login_capture.py + frontend/dist -> 母本 \\Sphgj\sphgj。"""
import hashlib
import os
import subprocess
import sys

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
sys.path.insert(0, os.path.join(ROOT, "tools"))
from unc_atomic import put_file, md5_of  # noqa: E402

FILES = ["backend/login_capture.py", "tools/patch_rev30.py"]


def git_show(path):
    try:
        p = subprocess.run(["git", "show", "HEAD:" + path.replace("\\", "/")],
                           cwd=ROOT, capture_output=True, check=True)
        return hashlib.md5(p.stdout).hexdigest()
    except Exception as e:
        return "<no-head:%s>" % e


def collect_dist():
    base = os.path.join(ROOT, "frontend", "dist")
    out = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            p = os.path.join(root, fn)
            out.append((p, "frontend/dist/" + os.path.relpath(p, base).replace("\\", "/")))
    return sorted(out)


def main():
    print("== 基线校验 ==")
    bad = 0
    for f in FILES:
        dst = os.path.join(DST, f)
        if not os.path.exists(dst):
            print("  %-32s 母本不存在(将新建)" % f)
            continue
        m_dst, m_head = md5_of(dst), git_show(f)
        ok = m_dst == m_head
        if not ok:
            bad += 1
        print("  %-32s %s" % (f, "一致" if ok else "不一致(母本=%s HEAD=%s)" % (m_dst[:8], m_head[:8])))
    if bad and "--force" not in sys.argv:
        print("\n[中止] %d 个文件不一致" % bad)
        return 2

    ok = 0
    print("\n== 后端 ==")
    for f in FILES:
        r = put_file(os.path.join(ROOT, f), os.path.join(DST, f))
        print("  %-32s %s" % (f, r))
        if r in ("ok", "skip"):
            ok += 1

    dist = collect_dist()
    dok = 0
    print("\n== 前端 dist %d 个 ==" % len(dist))
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
            print("  %-40s %s" % (rel, r))
    print("\n后端 %d/%d, 前端 %d/%d" % (ok, len(FILES), dok, len(dist)))
    return 0 if (ok == len(FILES) and dok == len(dist)) else 1


if __name__ == "__main__":
    sys.exit(main())
