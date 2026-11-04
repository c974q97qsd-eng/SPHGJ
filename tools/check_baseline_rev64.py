# -*- coding: utf-8 -*-
"""rev64 投递前基线校验 + 本地备份。

比对母本磁盘上的两个文件与本地当前(改动前)版本，归一化 CRLF->LF。
一致才允许投递，否则说明母本被别人改过，必须先查清楚。
"""
import io
import os
import shutil

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [r"backend\server.py", r"backend\post_fetcher.py"]


def norm(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n")


def main():
    bad = 0
    for rel in FILES:
        local = os.path.join(SRC, rel)
        remote = os.path.join(DST, rel)
        bak = local + ".bak-r64"
        if not os.path.exists(bak):
            shutil.copyfile(local, bak)
            print("  BACKUP %s" % bak)
        try:
            a, b = norm(local), norm(remote)
        except Exception as e:
            print("  ERR  读文件失败 %s: %s" % (rel, e))
            bad += 1
            continue
        same = a == b
        print("  %s %s%s" % ("SAME" if same else "DIFF", rel,
                             "" if same else "  (母本 %d / 本地 %d 字符)" % (len(b), len(a))))
        if not same:
            bad += 1
    print("BASELINE", "OK(母本=本地改动前版本，可投递)" if bad == 0
          else "FAIL(有 %d 处差异，先查清楚)" % bad)
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
