# -*- coding: utf-8 -*-
"""rev52 投递

backend/video_zone.py       新建:按播放增长分区(热区30/普通区80/冷区)
backend/storage.py          新增 video_activity 每日播放采样表 + 3 个方法
backend/api_client.py       post_list pageSize 默认 100;errCode 计入失败;write_blocked()
backend/comment_fetcher.py  分层扫描 + 请求重试 + 不完整告警(返回值 5 元组)
backend/account_manager.py  抓取去浏览器化(HTTP 优先)+ 探测未播间隔 300s + 引用计数配对

无前端改动;母本 config.json 无需修改(全部有默认值,要调再改)。
"""
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, r"D:\SPHGJ\tools")
from unc_atomic import put_file, md5_of   # noqa: E402

ROOT = r"D:\SPHGJ"
DST = r"\\Sphgj\sphgj"
PREV = "HEAD"

FILES = [
    "backend/video_zone.py",
    "backend/storage.py",
    "backend/api_client.py",
    "backend/comment_fetcher.py",
    "backend/account_manager.py",
]


def norm(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_raw(path):
    try:
        p = subprocess.run(["git", "show", "%s:%s" % (PREV, path.replace("\\", "/"))],
                           cwd=ROOT, capture_output=True, check=True)
        return p.stdout
    except Exception:
        return None


print("===== 基线校验(母本磁盘 vs 当前提交 %s) =====" % PREV)
bad = 0
for f in FILES:
    dst = os.path.join(DST, f.replace("/", os.sep))
    if not os.path.exists(dst):
        print("  %-30s 母本无此文件(新建) -> 跳过" % f)
        continue
    cur = open(dst, "rb").read()
    old = git_raw(f)
    if old is None:
        print("  %-30s 仓库无此文件 -> 跳过" % f)
        continue
    if norm(cur) == norm(old):
        print("  %-30s 一致 (%d 字节)" % (f, len(cur)))
    else:
        bad += 1
        print("  %-30s !! 内容不同 母本 %d / 仓库 %d 字节" % (f, len(cur), len(old)))

if bad:
    print("\n[中止] 有 %d 个文件母本版与仓库版不一致" % bad)
    sys.exit(1)

print("\n===== 投递 =====")
fail = 0
for f in FILES:
    src = os.path.join(ROOT, f.replace("/", os.sep))
    dst = os.path.join(DST, f.replace("/", os.sep))
    before = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    r = put_file(src, dst)
    after = md5_of(dst)[:8] if os.path.exists(dst) else "<无>"
    ok = after == md5_of(src)[:8]
    if not ok:
        fail += 1
    print("  %-30s %-5s 旧=%s 新=%s  %s"
          % (f, r, before, after, "OK" if ok else "!! 不一致"))

print("\n结果: 基线差异 %d 个, 投递失败 %d 个" % (bad, fail))
sys.exit(1 if fail else 0)
