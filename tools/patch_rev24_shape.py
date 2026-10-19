# -*- coding: utf-8 -*-
"""rev24 补丁 3.5:流式推送的评论对象带上 finder_id(前端 WS 插入要用)。"""
import io
import sys

P = r"D:\SPHGJ\backend\comment_fetcher.py"
s = io.open(P, encoding="utf-8").read()
items = [
    ('''def _shape(account_id, export_id, cmt):
    """把 API 原始评论对象整成前端友好的 dict。"""
    return {
        "account_id": account_id,''',
     '''def _shape(finder_id, account_id, export_id, cmt):
    """把 API 原始评论对象整成前端友好的 dict(归属 = finder_id 视频号)。"""
    return {
        "finder_id": finder_id,
        "account_id": account_id,'''),
    ('''                        new_list.append(_shape(self.account_id, export_id, cmt))''',
     '''                        new_list.append(_shape(self.finder_id, self.account_id, export_id, cmt))'''),
    ('''                            new_list.append(_shape(self.account_id, export_id, sub))''',
     '''                            new_list.append(_shape(self.finder_id, self.account_id, export_id, sub))'''),
]
fail = []
for old, new in items:
    if s.count(old) != 1:
        fail.append(old.splitlines()[0][:50])
        continue
    s = s.replace(old, new, 1)
if fail:
    print("PATCH FAIL", fail)
    sys.exit(1)
io.open(P, "w", encoding="utf-8", newline="").write(s)
s2 = io.open(P, encoding="utf-8").read()
ok = '"finder_id": finder_id' in s2 and s2.count("self.finder_id, self.account_id") == 2
print("SELFCHECK", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 2)
