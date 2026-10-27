# -*- coding: utf-8 -*-
"""rev50 步骤5: account_manager 建 LiveFetcher 时传入 account（dashboard 走 HTTP 的前提）。"""
import io
import sys

P = r"D:\SPHGJ\backend\account_manager.py"
s = io.open(P, encoding="utf-8").read()
orig = len(s)

REPL = [
    # 轻量探测解析用（无 page，不会碰 dashboard，传了也无害）
    ('            lf = LiveFetcher(None, self.account["id"])',
     '            lf = LiveFetcher(None, self.account["id"], account=self.account)'),
    # 开大屏页
    ('            lf = LiveFetcher(self.live_page, self.account["id"])',
     '            lf = LiveFetcher(self.live_page, self.account["id"], account=self.account)'),
    # 两处重建（多行，继承 learned）
    ('                        lf = LiveFetcher(self.live_page, self.account["id"],\n'
     '                                         learned=inherited_learned)',
     '                        lf = LiveFetcher(self.live_page, self.account["id"],\n'
     '                                         learned=inherited_learned,\n'
     '                                         account=self.account)'),
    ('                                lf = LiveFetcher(self.live_page, self.account["id"],\n'
     '                                                 learned=inherited_learned)',
     '                                lf = LiveFetcher(self.live_page, self.account["id"],\n'
     '                                                 learned=inherited_learned,\n'
     '                                                 account=self.account)'),
]

for old, new in REPL:
    n = s.count(old)
    if n != 1:
        print("FAIL 命中 %d 次: %s" % (n, old.strip().splitlines()[0][:70]))
        sys.exit(1)
    s = s.replace(old, new, 1)
    print("OK  " + old.strip().splitlines()[0][:70])

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = io.open(P, encoding="utf-8").read()
if chk.count("account=self.account") != 4:
    print("SELFCHECK FAIL: account=self.account 出现 %d 次，应为 4" % chk.count("account=self.account"))
    sys.exit(1)
print("OK  %d -> %d 字节" % (orig, len(chk)))
print("SELFCHECK PASS")
