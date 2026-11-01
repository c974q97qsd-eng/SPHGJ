# -*- coding: utf-8 -*-
"""rev62 投递前基线校验(3 个文件):母本 = 我改之前的 .bak-r62 备份?

归一化 CRLF->LF 再比(否则假报差异)。
"""
import io

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [r"backend\api_client.py", r"backend\auto_comment.py", r"backend\comment_fetcher.py"]


def norm(p):
    return io.open(p, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


bad = 0
for rel in FILES:
    try:
        base = norm(SRC + "\\" + rel + ".bak-r62")
        remote = norm(DST + "\\" + rel)
    except Exception as e:
        print("  ERR  %s: %s" % (rel, e))
        bad += 1
        continue
    same = base == remote
    print(("  SAME " if same else "  DIFF ") + rel
          + ("" if same else "  (母本 %d / 基线 %d 字符)" % (len(remote), len(base))))
    if not same:
        bad += 1

print("BASELINE", "OK(可直接投递)" if bad == 0 else "有 %d 处差异,先查清楚" % bad)
raise SystemExit(0 if bad == 0 else 1)
