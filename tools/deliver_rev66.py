# -*- coding: utf-8 -*-
"""rev66 投递：基线校验(母本磁盘 vs git HEAD,CRLF→LF 归一化) + put_file 两步 rename。

含前端 dist(vite 已重建),并把旧的 dist JS 改名让位 —— UNC 上删不掉文件,
只能纯 rename 搬走。
"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from unc_atomic import put_file, md5_of  # noqa: E402

SRC = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
FILES = [
    r"backend\local_window.py",
    r"backend\account_manager.py",
    r"backend\remote_view.py",
    r"backend\server.py",
    # 前端 src 不投母本:母本不构建前端,只跑 dist。实测母本 src 是 rev57 之前的
    # 历史残留(PlayDialog/RemotePage 根本没有),投过去反而制造假差异。
    r"frontend\dist\index.html",
    r"frontend\dist\assets\index-DpoYu5f0.js",
]
OLD_JS = r"frontend\dist\assets\index-DpCFPwBr.js"


def norm(b):
    return b.replace(b"\r\n", b"\n")


print("=== 基线校验(母本磁盘 vs git HEAD) ===")
bad = 0
for rel in FILES:
    git_rel = rel.replace("\\", "/")
    disk_path = os.path.join(DST, rel)
    try:
        head = subprocess.check_output(["git", "show", "HEAD:" + git_rel],
                                       cwd=SRC, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        head = None                       # 新增文件(dist 的 hash 名会变)
    if not os.path.exists(disk_path):
        print("  NEW %s (母本无此文件,将新建)" % rel)
        continue
    with io.open(disk_path, "rb") as f:
        disk = f.read()
    if head is None:
        print("  NEW %s (HEAD 无此文件,母本已有 -> 覆盖)" % rel)
        continue
    same = norm(head) == norm(disk)
    print("  %s %s head=%dB disk=%dB" % (
        "OK " if same else "BAD", rel, len(norm(head)), len(norm(disk))))
    if not same:
        bad += 1
if bad:
    print("BASELINE FAIL: 母本与 git HEAD 不一致,停止投递,先人工核对")
    raise SystemExit(1)
print("BASELINE PASS\n")

print("=== 投递 ===")
for rel in FILES:
    src = os.path.join(SRC, rel)
    dst = os.path.join(DST, rel)
    put_file(src, dst, tag="rev66 " + rel)
    ok = md5_of(dst) == md5_of(src)
    print("  %s %s md5=%s" % ("OK " if ok else "BAD", rel, md5_of(dst)[:12]))
    if not ok:
        bad += 1

# 旧 dist JS:纯 rename 让位(UNC 不能删)。index.html 已只引用新 hash 名。
old = os.path.join(DST, OLD_JS)
if os.path.exists(old):
    try:
        os.rename(old, old + ".retired-r66")
        print("  OK  旧 dist JS 已改名让位")
    except Exception as e:
        print("  WARN 旧 dist JS 改名失败(不影响运行): %s" % e)

print("DELIVER", "OK" if bad == 0 else "FAIL %d" % bad)
raise SystemExit(bad)
