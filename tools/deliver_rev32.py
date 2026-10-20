# -*- coding: utf-8 -*-
"""rev32 投递: account_manager.py + login_capture.py + server.py(仅后端)。"""
import hashlib
import os
import subprocess
import sys

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
sys.path.insert(0, os.path.join(ROOT, "tools"))
from unc_atomic import put_file, md5_of  # noqa: E402

FILES = ["backend/account_manager.py", "backend/login_capture.py",
         "backend/server.py", "tools/patch_rev32.py"]


def git_show(path):
    try:
        p = subprocess.run(["git", "show", "HEAD:" + path.replace("\\", "/")],
                           cwd=ROOT, capture_output=True, check=True)
        return hashlib.md5(p.stdout).hexdigest()
    except Exception as e:
        return "<no-head:%s>" % e


def main():
    print("== 基线校验 ==")
    bad = 0
    for f in FILES:
        dst = os.path.join(DST, f)
        if not os.path.exists(dst):
            print("  %-30s 母本不存在(将新建)" % f)
            continue
        m_dst, m_head = md5_of(dst), git_show(f)
        ok = m_dst == m_head
        if not ok:
            bad += 1
        print("  %-30s %s" % (f, "一致" if ok else "不一致(母本=%s HEAD=%s)" % (m_dst[:8], m_head[:8])))
    if bad and "--force" not in sys.argv:
        print("\n[中止] %d 个文件不一致" % bad)
        return 2
    ok = 0
    print("\n== 投递 ==")
    for f in FILES:
        r = put_file(os.path.join(ROOT, f), os.path.join(DST, f))
        print("  %-30s %s" % (f, r))
        if r in ("ok", "skip"):
            ok += 1
    print("\n完成 %d/%d" % (ok, len(FILES)))
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
