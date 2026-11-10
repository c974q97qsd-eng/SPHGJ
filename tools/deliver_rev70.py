# -*- coding: utf-8 -*-
"""rev70 投递:先做基线校验(母本磁盘 vs 本次改动前的本地副本),再 put_file。

基线校验的意义:母本可能被人手改过,直接覆盖会静默丢掉那些改动。
本地 .bak-r70 就是"本次改动前"的精确副本(补丁脚本写的),拿它当基线最准。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
MASTER = r"\\Sphgj\sphgj"

FILES = [
    "backend/auth_events.py",       # 新增
    "backend/account_manager.py",
    "backend/live_fetcher.py",
    "backend/live_probe_http.py",
    "backend/douyin/worker.py",
]


def norm(p):
    with io.open(p, "rb") as f:
        b = f.read()
    return b.replace(b"\r\n", b"\n")


def main():
    if not os.path.isdir(MASTER):
        print("FAIL 母本不可达: %s" % MASTER)
        return 2

    bad = []
    print("== 基线校验(母本磁盘 vs 改动前本地副本) ==")
    for rel in FILES:
        dst = os.path.join(MASTER, rel.replace("/", os.sep))
        bak = os.path.join(ROOT, rel.replace("/", os.sep) + ".bak-r70")
        if not os.path.exists(bak):
            print("  [NEW ] %s 本地为新增文件,母本存在=%s"
                  % (rel, os.path.exists(dst)))
            continue
        if not os.path.exists(dst):
            print("  [MISS] %s 母本不存在(会被新建)" % rel)
            continue
        a, b = norm(dst), norm(bak)
        if a == b:
            print("  [SAME] %s" % rel)
        else:
            print("  [DIFF] %s 母本与改动前基线不一致(母本被人改过?)" % rel)
            bad.append(rel)

    if bad:
        print("\n基线不一致,已中止投递: %s" % bad)
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
        if os.path.exists(dst) and md5_of(lp) != md5_of(dst):
            print("  [MISMATCH] %s md5 不一致" % rel)
            fails.append(rel)

    print("\n结果: %s" % ("ALL OK" if not fails else "FAILED %s" % fails))
    return 0 if not fails else 4


if __name__ == "__main__":
    sys.exit(main())
