# -*- coding: utf-8 -*-
"""rev25-fix2: 失败不再归档文件(否则点「重试」时源文件已被搬走)。

设计修正:
  - 成功 -> 移到 inbox/_done/(原件不在 inbox,不会被重复扫描)
  - 失败 -> 文件留在原地(任务标 failed;因 (finder,file_fp) 唯一键,重扫不会重复入队,
            点「重试」时源文件还在,能直接重发)
  - 扫描时跳过所有 "_" 开头的目录(_done/_failed/_unknown 都不是视频号目录)
"""
import io, sys

P = r"D:\SPHGJ\backend\publish_queue.py"
s = io.open(P, encoding="utf-8").read()

# 1) 扫描时跳过 _ 开头的目录
OLD1 = """            if not os.path.isdir(sub) or name in (SUB_DONE, SUB_FAILED):
                continue
            if name.startswith("."):
                continue"""
NEW1 = """            if not os.path.isdir(sub):
                continue
            # _done/_failed/_unknown 等辅助目录都不是视频号目录,一律跳过
            if name.startswith("_") or name.startswith("."):
                continue"""
assert s.count(OLD1) == 1, "扫描跳过锚点不唯一"
s = s.replace(OLD1, NEW1, 1)

# 2) 失败不归档
OLD2 = """                # 没发出去 -> 标题还回池子,下次还能用
                if t.get("title_id"):
                    self.storage.release_title(t["title_id"])
                self._archive(t, ok=False)"""
NEW2 = """                # 没发出去 -> 标题还回池子,下次还能用
                if t.get("title_id"):
                    self.storage.release_title(t["title_id"])
                # 刻意不移动源文件:留着才能点「重试」;同 (finder,fp) 唯一键保证不会再入队"""
assert s.count(OLD2) == 1, "失败分支锚点不唯一"
s = s.replace(OLD2, NEW2, 1)

# 3)_archive 只在成功时调用(去掉失败路径的语义)
OLD3 = '''    def _archive(self, task, ok):
        """归档:成功移到 _done/,失败移到 _failed/(保留文件,方便人工复核后重试)。"""'''
NEW3 = '''    def _archive(self, task, ok=True):
        """成功后把源文件移进 _done/(失败不移动,见 run_task 里的说明)。"""'''
assert s.count(OLD3) == 1, "_archive 锚点不唯一"
s = s.replace(OLD3, NEW3, 1)

OLD4 = """        sub = os.path.join(root, SUB_DONE if ok else SUB_FAILED)"""
NEW4 = """        sub = os.path.join(root, SUB_DONE if ok else SUB_FAILED)  # 目前只有成功路径会调用"""
assert s.count(OLD4) == 1
s = s.replace(OLD4, NEW4, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
chk = io.open(P, encoding="utf-8").read()
miss = [m for m in ("name.startswith(\"_\")", "刻意不移动源文件", "def _archive(self, task, ok=True)") if m not in chk]
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
