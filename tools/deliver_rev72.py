# -*- coding: utf-8 -*-
"""rev72 投递:母本磁盘 vs git HEAD 基线校验(CRLF→LF 归一化)后 put_file。

本次改 3 个后端文件,不动前端 dist / 不动 config.json(吃默认 http)。
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

FILES = [
    "backend/api_client.py",
    "backend/account_manager.py",
    "backend/server.py",
]


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
    for rel in FILES:
        dst = os.path.join(MASTER, rel.replace("/", os.sep))
        if not os.path.exists(dst):
            print("  [MISS] %s 母本不存在" % rel)
            bad.append(rel)
            continue
        a = norm(io.open(dst, "rb").read())
        b = norm(git_head_bytes(rel))
        same = a == b
        print("  [%s] %-32s master=%d HEAD=%d" %
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
        try:
            r = put_file(lp, dst, tag=rel)
            print("  [%s] %s" % (r.upper(), rel))
        except Exception as e:
            print("  [FAIL] %s -> %r" % (rel, e))
            fails.append(rel)
            continue
        if md5_of(lp) != md5_of(dst):
            print("  [MISMATCH] %s md5 不一致" % rel)
            fails.append(rel)

    print("\n结果: %s" % ("ALL OK" if not fails else "FAILED %s" % fails))
    return 0 if not fails else 4


if __name__ == "__main__":
    sys.exit(main())
