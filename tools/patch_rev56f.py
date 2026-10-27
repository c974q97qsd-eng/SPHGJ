# -*- coding: utf-8 -*-
"""rev56f：给第 2 步真机验证补一段「加密 item_id 覆盖率」断言。

这个断言直接锁死 rev56d 修的那个阻塞项（读错游标字段 -> 只覆盖 19 条）。
以后谁再把 cursor 改回 max_cursor，这里立刻红。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VER = os.path.join(ROOT, "tools", "dyprobe", "verify_step2.py")
MGR = os.path.join(ROOT, "backend", "douyin", "manager.py")

anchor = '''        print("\\n[2] 第一轮抓取（限 3 个作品，避免刷太多请求）")'''

add = '''        print("\\n[1b] 加密 item_id 覆盖率（rev56d 修的阻塞项，必须接近 100%）")
        works_all, _ = await dy._client.fetch_all_works()
        all_ids = [w["aweme_id"] for w in works_all if w.get("aweme_id")]
        private = [w for w in works_all if str(w.get("status_value")) in ("140", "141")]
        mp = await dy._client.map_item_ids(all_ids)
        pub = [x for x in all_ids
               if x not in {w["aweme_id"] for w in private}]
        cov = len([x for x in pub if x in mp])
        print("      作品 %d 条（其中非公开 %d 条）-> 映射 %d 条"
              % (len(all_ids), len(private), len(mp)))
        ck(len(all_ids) > 0, "读到作品全集", len(all_ids))
        ck(len(mp) >= len(pub), "所有公开作品都拿到了加密 item_id",
           "%d / %d" % (cov, len(pub)))
        ck(len(mp) <= len(all_ids), "映射条数不超过作品总数（没有串号）", len(mp))
        # 缓存生效：第二次应该零请求（用调用次数间接验）
        mp2 = await dy._client.map_item_ids(all_ids)
        ck(mp2 == mp, "第二次映射结果一致（命中缓存）")

        print("\\n[2] 第一轮抓取（限 3 个作品，避免刷太多请求）")'''

s = io.open(VER, encoding="utf-8").read()
assert s.count(anchor) == 1, "验证脚本锚点命中 %d 次" % s.count(anchor)
s = s.replace(anchor, add, 1)
io.open(VER, "w", encoding="utf-8", newline="").write(s)

# manager.set_page 补一句说明：缓存随 client 重建，天然不跨账号
old = '''        self._client = DouyinClient(page, self.account(), config=self.config,
                                    storage=self.storage)'''
new = '''        # 每次绑定都新建 client，所以加密 item_id 缓存（_item_enc）随旧 client
        # 一起丢掉 —— 换账号/重连时不会拿着上一账号的加密串去打接口。
        self._client = DouyinClient(page, self.account(), config=self.config,
                                    storage=self.storage)'''
m = io.open(MGR, encoding="utf-8").read()
assert m.count(old) == 1
m = m.replace(old, new, 1)
io.open(MGR, "w", encoding="utf-8", newline="").write(m)

import py_compile  # noqa: E402
for p in (VER, MGR):
    py_compile.compile(p, doraise=True)
    print("语法 OK " + os.path.basename(p))
print("补丁完成")
