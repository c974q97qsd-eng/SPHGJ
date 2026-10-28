# -*- coding: utf-8 -*-
"""rev41 补丁：发布成功归档跟着「店铺目录」走（不再一律塞 inbox/_done/）。

问题
----
scan_inbox() 把 `inbox/<目录名>/` 当作一个店铺（视频号）目录，`_` 开头的一律跳过；
而 _archive() 是 `os.path.join(inbox_dir, "_done")` —— 归档到 inbox 根下一级。
结果：不同店铺的素材全混在 `inbox/_done/` 里，归档后不再按店铺区分，
人工翻 _done 判断「这家店发过什么」时无从下手。现场的标准结构其实是
`inbox/<店名>/_done/`。

改法
----
新增 _archive_subdir(src, ok)：按**源文件所在目录**算归档目的地。
  * src 在 `inbox/<店名>/` 下        -> `inbox/<店名>/_done/`
  * src 直接在 inbox 根              -> 退回 `inbox/_done/`（保持旧行为）
  * src 不在当前 inbox 之下          -> 退回 `inbox/_done/`（改过 inbox_dir /
                                        历史别处路径 / 跨盘，都不能往用户别处乱建目录）
  * src 已在 `_done`/`_failed`/... 里 -> 返回原地（重复归档视为已完成，不再搬）
_published_name 增加 dest_dir 参数：目标目录多了一级店名，Windows 260 路径
上限的可用长度要按**实际目标目录**算，否则长标题会拼出超长路径导致归档失败。

幂等：脚本可重复执行；已应用则打印提示后退出。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "backend", "publish_queue.py")

NEW_ARCHIVE = '''    def _archive_subdir(self, src, ok=True):
        """算出 src 该归档到哪个目录 —— 跟着**源文件所在的店铺目录**走。

        rev41:旧行为一律丢进 inbox/_done/(inbox 根下一级),而 scan_inbox 是把
        inbox/<目录名>/ 当店铺目录的,于是归档后素材不再按店铺区分,人工翻 _done
        看不出是哪家店发的。现在归到 <店铺目录>/_done/。

        返回 (目标目录, 是否贴合店铺目录)。退回 inbox/<_done|_failed> 的情形:
          * src 为空 / 不在当前 inbox 之下(改过 inbox_dir、历史别处的路径、跨盘)
            —— 绝不能因为源文件在别处就往用户别处建 _done 目录
          * src 直接在 inbox 根(不是店铺目录)
        src 已经在 _done/_failed/_unknown 里时返回其父目录本身(原地,视为已归档)。
        """
        root = os.path.abspath(self.inbox_dir)
        fallback = os.path.join(root, SUB_DONE if ok else SUB_FAILED)
        if not src:
            return fallback, False
        parent = os.path.dirname(os.path.abspath(src))
        try:
            inside = (os.path.normcase(os.path.commonpath([root, parent]))
                      == os.path.normcase(root))
        except ValueError:      # 不同盘符/UNC 不同共享 -> commonpath 抛 ValueError
            inside = False
        if not inside or os.path.normcase(parent) == os.path.normcase(root):
            return fallback, False
        name = os.path.basename(parent)
        if not name:
            return fallback, False
        if name.startswith("_") or name.startswith("."):
            # 已经在 _done/_failed/_unknown 等辅助目录里:原地即归档结果
            return parent, False
        return os.path.join(parent, SUB_DONE if ok else SUB_FAILED), True

    def _archive(self, task, ok=True):
        """把源文件移进 <店铺目录>/_done/。返回归档后的文件名(失败返回 "")。

        rev26:开启 rename_after_publish 时按用户要求的格式重命名:
            "发布日期"发布-"使用的标题"-原文件名
        例:2026-10-20发布-厚底老爹鞋视觉增高 比例一下就出来了-2月28日 (58).mp4
        rev41:归档目录跟着源文件所在店铺走(inbox/<店名>/_done/),不再一律塞
            inbox/_done/。店铺目录里没视频可扫时 scan_inbox 会跳过它,不影响认领。
        """
        src = task.get("src_path") or ""
        if not src or not os.path.isfile(src):
            return ""
        sub, in_shop = self._archive_subdir(src, ok)
        # 源文件已经在目标目录里(重复归档 / 已归档过) -> 原地就是结果,不再搬
        if os.path.normcase(os.path.dirname(os.path.abspath(src))) == os.path.normcase(sub):
            return os.path.basename(src)
        try:
            os.makedirs(sub, exist_ok=True)
            name = os.path.basename(src)
            if ok and self._cfg_get("rename_after_publish"):
                name = self._published_name(task, src, sub)
            dst = _unique_path(os.path.join(sub, name))
            shutil.move(src, dst)
            if not in_shop:
                # 退回到 inbox 根:源文件不在店铺目录下,留线索便于排查
                logger.warning(f"[publish] 归档退回到 inbox 根(源文件不在店铺目录下) "
                               f"{src} -> {dst}")
            return os.path.basename(dst)
        except Exception as e:
            logger.warning(f"[publish] 归档文件失败 {src}: {e}")
            return ""

'''


def patch():
    raw = io.open(PATH, "rb").read().decode("utf-8")
    crlf = "\r\n" in raw
    s = raw.replace("\r\n", "\n")

    if "def _archive_subdir" in s:
        print("rev41 已应用过（backend/publish_queue.py 里已有 _archive_subdir），跳过。")
        return 0

    # --- 1) 整段替换 _archive ---
    a = s.index("    def _archive(self, task, ok=True):")
    b = s.index("    def _published_name(self, task, src):")
    old_block = s[a:b]
    assert "os.path.join(root, SUB_DONE if ok else SUB_FAILED)" in old_block, \
        "旧 _archive 锚点不符（找不到 inbox 根 _done 那行）"
    assert "def _published_name" not in old_block
    s = s[:a] + NEW_ARCHIVE + s[b:]

    # --- 2) _published_name 支持 dest_dir ---
    old_sig = "    def _published_name(self, task, src):\n" \
              '        """拼 "日期发布-标题-原文件名"。标题清洗为合法文件名,整体长度受控。"""'
    new_sig = "    def _published_name(self, task, src, dest_dir=None):\n" \
              '        """拼 "日期发布-标题-原文件名"。标题清洗为合法文件名,整体长度受控。\n\n' \
              '        rev41:dest_dir 是实际归档目录(含店铺名)。260 路径上限要按它算,\n' \
              '        否则目标目录多一级店名后,长标题会拼出超长路径导致归档失败。\n' \
              '        """'
    assert s.count(old_sig) == 1, "改前签名锚点命中 %d 次" % s.count(old_sig)
    s = s.replace(old_sig, new_sig, 1)

    old_room = "        room = 250 - len(os.path.join(self.inbox_dir, SUB_DONE))"
    new_room = ("        room = 250 - len(dest_dir or os.path.join(self.inbox_dir, SUB_DONE))")
    assert s.count(old_room) == 1, "room 锚点命中 %d 次" % s.count(old_room)
    s = s.replace(old_room, new_room, 1)

    # --- 3) 文件头注释补一句 ---
    old_c = "    # 发布成功后把素材重命名为 \"发布日期\"发布-\"标题\"-原文件名(归档到 _done/)"
    new_c = "    # 发布成功后把素材重命名为 \"发布日期\"发布-\"标题\"-原文件名\n" \
            "    # (rev41:归档到 <店铺目录>/_done/,不再一律塞 inbox/_done/)"
    assert s.count(old_c) == 1, "文件头注释锚点命中 %d 次" % s.count(old_c)
    s = s.replace(old_c, new_c, 1)

    out = s.replace("\n", "\r\n") if crlf else s
    io.open(PATH, "wb").write(out.encode("utf-8"))
    return 1


def selfcheck():
    t = io.open(PATH, "rb").read().decode("utf-8")
    checks = [
        ("_archive_subdir 已定义", "def _archive_subdir(self, src, ok=True):" in t),
        ("_archive 调用 _archive_subdir", '_archive_subdir(src, ok)' in t),
        ("原地归档短路", "== os.path.normcase(sub):" in t),
        ("published_name 收 dest_dir", "def _published_name(self, task, src, dest_dir=None):" in t),
        ("room 用 dest_dir", "room = 250 - len(dest_dir or" in t),
        ("同名 (2) 不覆盖逻辑仍用 dst", "dst = _unique_path(os.path.join(sub, name))" in t),
        ("退回根的 warning 保留", "归档退回到 inbox 根" in t),
        ("旧的无条件归档已消失",
         'sub = os.path.join(root, SUB_DONE if ok else SUB_FAILED)  # 目前只有成功路径会调用' not in t),
        ("_archive 只有一个调用点仍是 ok=True", t.count("self._archive(t, ok=True)") == 1),
    ]
    ok = True
    for name, cond in checks:
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond
    import py_compile
    try:
        py_compile.compile(PATH, doraise=True)
        print("  PASS 语法检查")
    except Exception as e:
        print("  FAIL 语法检查:", e)
        ok = False
    print("SELFCHECK", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    print("=== 1. 应用补丁 ===")
    n = patch()
    print("  改动", n, "处" if n else "(无)")
    print("=== 2. 自校验 ===")
    sys.exit(0 if selfcheck() else 1)
