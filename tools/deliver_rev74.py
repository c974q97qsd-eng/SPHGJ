# -*- coding: utf-8 -*-
"""rev74 投递：backend/remote_view.py + backend/server.py。

先做基线校验（母本磁盘 vs git HEAD，CRLF→LF 归一化），再用 unc_atomic.put_file
两步纯 rename 投递，最后做 md5 复核。前端 src/dist 本次完全不动（占用提示复用
现有 t=error 分支），母本也不构建前端。
"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
MASTER = r"\\Sphgj\sphgj"
GIT = r"D:\Program Files\Git\cmd\git.exe"
FILES = ["backend/remote_view.py", "backend/server.py"]
# 增量补投时母本已不等于 git HEAD（上一版刚投过），用 --force 跳过基线校验，
# 仍会做投递后的 md5 复核。
FORCE = "--force" in sys.argv


def norm(b):
    return b.replace(b"\r\n", b"\n")


def git_head_bytes(rel):
    p = subprocess.run([GIT, "show", "HEAD:" + rel], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.returncode == 0, "git show 失败: %s" % p.stderr.decode("utf-8", "replace")[:200]
    return p.stdout


def main():
    if not os.path.isdir(MASTER):
        print("FAIL 母本不可达: %s" % MASTER)
        return 2

    bad = []
    print("== 基线校验(母本磁盘 vs git HEAD, CRLF→LF 归一化) ==")
    if FORCE:
        print("  [SKIP] --force: 增量补投,母本已是上一版 rev74,跳过基线校验")
    for rel in ([] if FORCE else FILES):
        dst = os.path.join(MASTER, rel.replace("/", os.sep))
        a = norm(io.open(dst, "rb").read())
        b = norm(git_head_bytes(rel))
        same = a == b
        print("  [%s] %-28s master=%d HEAD=%d" %
              ("SAME" if same else "DIFF", rel, len(a), len(b)))
        if not same:
            bad.append(rel)
    if bad:
        print("\n基线不一致,已中止投递(母本可能被手改): %s" % bad)
        return 3

    print("\n== 投递 ==")
    fails = []
    for rel in FILES:
        lp = os.path.join(ROOT, rel.replace("/", os.sep))
        dst = os.path.join(MASTER, rel.replace("/", os.sep))
        r = put_file(lp, dst, tag=rel)
        print("  [%s] %s" % (r.upper(), rel))
        if md5_of(lp) != md5_of(dst):
            print("  [MISMATCH] %s md5 不一致" % rel)
            fails.append(rel)
    print("\n结果: %s" % ("ALL OK" if not fails else "FAILED %s" % fails))
    return 0 if not fails else 4


if __name__ == "__main__":
    sys.exit(main())
