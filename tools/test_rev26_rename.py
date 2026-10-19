# -*- coding: utf-8 -*-
"""rev26 自测:发布后素材重命名格式与安全边界。"""
import os
import sys

sys.path.insert(0, r"D:\SPHGJ")
from backend.publish_queue import PublishQueue, _safe_name, _unique_path  # noqa: E402

q = PublishQueue(None, None, {"publish": {}}, None)
print("inbox:", q.inbox_dir, "| rename 默认:", q._cfg_get("rename_after_publish"))

cases = [
    ("2月28日 (58).mp4", "厚底老爹鞋视觉增高 比例一下就出来了"),
    ("2月28日 (59).mp4", "一双厚底鞋顶三双平底出门不用垫增高 #男鞋 #老爹鞋"),
    ('a/b:c*d?e"f<g>h|i.mp4', "带非法字符的标题:测试/斜杠"),
    ("no-title.mp4", "   "),
    ("long.mp4", "很长的标题" * 30),
]
for src, title in cases:
    got = q._published_name({"title": title}, os.path.join("D:\\x", src))
    print(f"  {got}")
    assert "发布-" in got, got
    assert not any(ch in got for ch in '\\/:*?"<>|'), f"残留非法字符: {got}"
    assert len(got) <= 220, f"过长 {len(got)}: {got}"
    assert got.endswith(".mp4"), got

# 重名绝不覆盖
import glob
for old in glob.glob(os.path.join(os.environ.get("TEMP", "."), "_rev26_dup*")):
    os.remove(old)
p = os.path.join(os.environ.get("TEMP", "."), "_rev26_dup.mp4")
open(p, "wb").write(b"x")
u1 = _unique_path(p)
assert u1 != p and "(2)" in u1, u1
open(u1, "wb").write(b"x")
u2 = _unique_path(p)
assert "(3)" in u2, u2
print("  重名序号:", os.path.basename(u1), os.path.basename(u2))
for f in glob.glob(os.path.join(os.environ.get("TEMP", "."), "_rev26_dup*")):
    os.remove(f)

print("\n[PASS] rev26 自测全过")
