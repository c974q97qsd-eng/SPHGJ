# -*- coding: utf-8 -*-
"""投递前基线校验：母本磁盘上的文件，是否仍等于我改之前的那份(.bak-r61)。

归一化 CRLF->LF 再比（否则假报差异）。
"""
import io

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = ["backend\\api_client.py", "backend\\server.py", "backend\\auto_reply.py",
         "backend\\auto_comment.py", "backend\\storage.py"]


def norm(p):
    s = io.open(p, encoding="utf-8", errors="replace").read()
    return s.replace("\r\n", "\n")


bad = 0
for rel in FILES:
    base = norm(SRC + "\\" + rel + ".bak-r61")
    if "\\" in rel:
        name, ext = rel.rsplit(".", 1)
        pass
    # .bak-r61 实际文件名是 xxx.py.bak-r61
    try:
        remote = norm(DST + "\\" + rel)
    except Exception as e:
        print("  ERR  读母本失败 %s: %s" % (rel, e))
        bad += 1
        continue
    same = base == remote
    print(("  SAME " if same else "  DIFF ") + rel
          + ("" if same else "  (母本 %d 字符 / 基线 %d 字符)" % (len(remote), len(base))))
    if not same:
        bad += 1

print("BASELINE", "OK(母本未被改过,可直接投递)" if bad == 0 else "有 %d 处差异,先查清楚" % bad)
