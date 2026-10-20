# -*- coding: utf-8 -*-
"""rev28 投递: 4 个后端文件 -> 母本 \\Sphgj\sphgj (两步 rename)。

投递前做基线校验(母本磁盘版 vs 本地 HEAD 前一版),避免覆盖掉别人在母本上的改动。
"""
import os
import subprocess
import sys

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
sys.path.insert(0, os.path.join(ROOT, "tools"))
from unc_atomic import put_file, md5_of  # noqa: E402

FILES = ["backend/browser.py", "backend/login_capture.py",
         "backend/account_manager.py", "backend/server.py",
         "tools/patch_rev28.py"]


def git_show(path):
    """工作区文件的 HEAD 版本 md5(用于确认母本没被单独改过)。"""
    try:
        p = subprocess.run(["git", "show", "HEAD:" + path.replace("\\", "/")],
                           cwd=ROOT, capture_output=True, check=True)
        import hashlib
        return hashlib.md5(p.stdout).hexdigest()
    except Exception as e:
        return f"<no-head:{e}>"


def main():
    print("== 基线校验(母本 vs 本地 HEAD)==")
    bad = 0
    for f in FILES:
        src = os.path.join(ROOT, f)
        dst = os.path.join(DST, f)
        if not os.path.exists(dst):
            print(f"  {f:34s} 母本不存在(将新建)")
            continue
        m_dst = md5_of(dst)
        m_head = git_show(f)
        state = "一致" if m_dst == m_head else f"不一致(母本={m_dst[:8]} HEAD={str(m_head)[:8]})"
        if m_dst != m_head:
            bad += 1
        print(f"  {f:34s} {state}")

    if bad and "--force" not in sys.argv:
        print(f"\n[中止] {bad} 个文件母本与 HEAD 不一致,先确认再投递(加 --force 跳过)")
        return 2

    print("\n== 投递 ==")
    ok = 0
    for f in FILES:
        src = os.path.join(ROOT, f)
        dst = os.path.join(DST, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = put_file(src, dst)
        print(f"  {f:34s} {r}")
        if r in ("ok", "skip"):
            ok += 1
    print(f"\n完成 {ok}/{len(FILES)}")
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
