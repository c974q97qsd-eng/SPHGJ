# -*- coding: utf-8 -*-
"""rev63 投递前基线校验:母本磁盘文件 == 我改之前的 .bak-r63 备份?

归一化 CRLF->LF 再比(否则假报差异)。
"""
import io

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [
    r"backend\publish_queue.py",
    r"backend\server.py",
    r"frontend\src\lib\api.ts",
    r"frontend\src\components\publish\SettingsPanel.tsx",
]


def norm(p):
    return io.open(p, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")


bad = 0
for rel in FILES:
    try:
        base = norm(SRC + "\\" + rel + ".bak-r63")
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
