# -*- coding: utf-8 -*-
"""rev26: 发布成功后按「日期发布-标题-原文件名」重命名素材。

用户要求格式:
    "当前发布日期"发布-"使用的标题"-原文件名
例: 2026-10-20发布-厚底老爹鞋视觉增高 比例一下就出来了-2月28日 (58).mp4

涉及:
  storage.py        publish_tasks 加 archived_name 列(新库 DDL + 老库补列);
                    set_task_status 支持写 archived_name
  publish_queue.py  DEFAULTS 加 rename_after_publish; _archive 实现安全改名
                    (非法字符清洗 / 标题截断 / 重名加序号 / 路径超长回退)
  SettingsPanel.tsx 「发布策略」加开关
"""
import io
import os
import sys

LOCAL = r"D:\SPHGJ"
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# ---------------- 1. storage.py: 新库 DDL 加列 ----------------
add(r"backend\storage.py",
    """            started_at TEXT,
            finished_at TEXT,
            UNIQUE(finder_id, file_fp))\"\"\")""",
    """            started_at TEXT,
            finished_at TEXT,
            archived_name TEXT DEFAULT '',
            UNIQUE(finder_id, file_fp))\"\"\")
        # 老库补列(rev26):归档后的文件名,便于回溯「这条发的是哪个文件」
        try:
            c.execute("ALTER TABLE publish_tasks ADD COLUMN archived_name TEXT DEFAULT ''")
        except Exception:
            pass""",
    "storage: DDL 加 archived_name + 老库补列")

# ---------------- 2. storage.py: set_task_status 支持 archived_name ----------------
add(r"backend\storage.py",
    """    def set_task_status(self, task_id, status=None, step=None, error=None, object_id=None,
                        export_id=None, account_id=None, started=False, finished=False,
                        attempts=None):
        sets, args = [], []
        for col, val in (("status", status), ("step", step), ("last_error", error),
                         ("object_id", object_id), ("export_id", export_id),
                         ("account_id", account_id), ("attempts", attempts)):""",
    """    def set_task_status(self, task_id, status=None, step=None, error=None, object_id=None,
                        export_id=None, account_id=None, started=False, finished=False,
                        attempts=None, archived_name=None):
        sets, args = [], []
        for col, val in (("status", status), ("step", step), ("last_error", error),
                         ("object_id", object_id), ("export_id", export_id),
                         ("account_id", account_id), ("attempts", attempts),
                         ("archived_name", archived_name)):""",
    "storage: set_task_status 支持 archived_name")

# ---------------- 3. publish_queue.py: 配置项 ----------------
add(r"backend\publish_queue.py",
    """    "refresh_posts_after_sec": 180,  # 发完多久触发一次作品列表刷新
}""",
    """    "refresh_posts_after_sec": 180,  # 发完多久触发一次作品列表刷新
    # 发布成功后把素材重命名为 "发布日期"发布-"标题"-原文件名(归档到 _done/)
    "rename_after_publish": True,
}""",
    "queue: DEFAULTS 加 rename_after_publish")

# ---------------- 4. publish_queue.py: _archive 改名 ----------------
add(r"backend\publish_queue.py",
    '''    def _archive(self, task, ok=True):
        """成功后把源文件移进 _done/(失败不移动,见 run_task 里的说明)。"""
        src = task.get("src_path") or ""
        if not src or not os.path.isfile(src):
            return
        root = self.inbox_dir
        sub = os.path.join(root, SUB_DONE if ok else SUB_FAILED)  # 目前只有成功路径会调用
        try:
            os.makedirs(sub, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = os.path.join(sub, f"{stamp}_{os.path.basename(src)}")
            shutil.move(src, dst)
        except Exception as e:
            logger.warning(f"[publish] 归档文件失败 {src}: {e}")''',
    '''    def _archive(self, task, ok=True):
        """成功后把源文件移进 _done/。返回归档后的文件名(失败返回 "")。

        rev26:开启 rename_after_publish 时按用户要求的格式重命名:
            "发布日期"发布-"使用的标题"-原文件名
        例:2026-10-20发布-厚底老爹鞋视觉增高 比例一下就出来了-2月28日 (58).mp4
        """
        src = task.get("src_path") or ""
        if not src or not os.path.isfile(src):
            return ""
        root = self.inbox_dir
        sub = os.path.join(root, SUB_DONE if ok else SUB_FAILED)  # 目前只有成功路径会调用
        try:
            os.makedirs(sub, exist_ok=True)
            name = os.path.basename(src)
            if ok and self._cfg_get("rename_after_publish"):
                name = self._published_name(task, src)
            dst = _unique_path(os.path.join(sub, name))
            shutil.move(src, dst)
            return os.path.basename(dst)
        except Exception as e:
            logger.warning(f"[publish] 归档文件失败 {src}: {e}")
            return ""

    def _published_name(self, task, src):
        """拼 "日期发布-标题-原文件名"。标题清洗为合法文件名,整体长度受控。"""
        base = os.path.basename(src)
        stem, ext = os.path.splitext(base)
        title = (task.get("title") or "").strip()
        # 标题里的换行/制表符压成空格,话题 # 保留(它是标题的一部分)
        title = re.sub(r"\\s+", " ", title).strip()
        title = _safe_name(title)[:60].strip(" .-_")
        date = datetime.now().strftime("%Y-%m-%d")
        if title:
            name = f"{date}发布-{title}-{stem}{ext}"
        else:
            name = f"{date}发布-{stem}{ext}"
        # Windows 路径上限 260:超长时截断标题而不是截断原文件名(原名是溯源关键)
        room = 250 - len(os.path.join(self.inbox_dir, SUB_DONE))
        if len(name) > room:
            over = len(name) - room
            keep = max(8, len(title) - over - 1)
            name = f"{date}发布-{title[:keep].strip(' .-_')}-{stem}{ext}"
            if len(name) > room:
                name = f"{date}发布-{stem}{ext}"
        return name''',
    "queue: _archive 按日期+标题重命名")

# ---------------- 5. publish_queue.py: 成功分支回写 archived_name ----------------
add(r"backend\publish_queue.py",
    '''                self._fail_streak[fid] = 0
                self._archive(t, ok=True)
                logger.info(f"[publish] 发布成功 {t.get('file_name')} -> {self.finder_label(fid)}"
                            f" object_id={res.get('object_id')}")''',
    '''                self._fail_streak[fid] = 0
                archived = self._archive(t, ok=True)
                if archived:
                    self.storage.set_task_status(int(task_id), archived_name=archived)
                logger.info(f"[publish] 发布成功 {t.get('file_name')} -> {self.finder_label(fid)}"
                            f" object_id={res.get('object_id')}"
                            + (f" | 已归档为 {archived}" if archived else ""))''',
    "queue: 成功分支回写 archived_name")

# ---------------- 6. publish_queue.py: 工具函数 ----------------
add(r"backend\publish_queue.py",
    """import random
import shutil""",
    """import random
import re
import shutil""",
    "queue: import re")

add(r"backend\publish_queue.py",
    '''def _hhmm_now():
    return datetime.now().strftime("%H:%M")''',
    '''def _hhmm_now():
    return datetime.now().strftime("%H:%M")


_BAD_NAME_CHARS = re.compile(r'[\\\\/:*?"<>|\\r\\n\\t]')


def _safe_name(s):
    """清掉 Windows 文件名非法字符(换成空格)并压缩连续空格。"""
    s = _BAD_NAME_CHARS.sub(" ", s or "")
    return re.sub(r"\\s{2,}", " ", s).strip()


def _unique_path(path):
    """目标已存在时加 (2)(3) 序号,绝不覆盖已有归档文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
    return f"{stem} (dup){ext}"''',
    "queue: 新增 _safe_name / _unique_path")

# ---------------- 7. SettingsPanel.tsx: 加开关 ----------------
add(r"frontend\src\components\publish\SettingsPanel.tsx",
    """        auto_start_account: !!c.auto_start_account,""",
    """        auto_start_account: !!c.auto_start_account,
        rename_after_publish: c.rename_after_publish !== false,""",
    "设置面板: 载入新开关")

add(r"frontend\src\components\publish\SettingsPanel.tsx",
    """          <Row label="账号未运行时自动拉起" hint="关闭则只发正在运行的账号,其余排队等待">
            <Switch checked={!!form.auto_start_account} onCheckedChange={(v) => set("auto_start_account", v)} />
          </Row>""",
    """          <Row label="账号未运行时自动拉起" hint="关闭则只发正在运行的账号,其余排队等待">
            <Switch checked={!!form.auto_start_account} onCheckedChange={(v) => set("auto_start_account", v)} />
          </Row>
          <Row label="发布成功后重命名素材"
            hint={`归档到 _done 时改名为「日期发布-标题-原文件名」,例:2026-10-20发布-厚底老爹鞋视觉增高-原文件名.mp4`}>
            <Switch checked={form.rename_after_publish !== false}
              onCheckedChange={(v) => set("rename_after_publish", v)} />
          </Row>""",
    "设置面板: 加开关行")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        s = s.replace(old, new, 1)
        io.open(p, "w", encoding="utf-8", newline="").write(s)
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")

    # ---- 自校验 ----
    print("\n== 自校验 ==")
    checks = [
        (r"backend\storage.py", 'archived_name TEXT DEFAULT \'\'', "storage 新库 DDL"),
        (r"backend\storage.py", 'ALTER TABLE publish_tasks ADD COLUMN archived_name', "storage 补列"),
        (r"backend\storage.py", 'attempts=None, archived_name=None', "storage 签名"),
        (r"backend\publish_queue.py", '"rename_after_publish": True', "默认开关"),
        (r"backend\publish_queue.py", "def _published_name", "改名函数"),
        (r"backend\publish_queue.py", "def _safe_name", "清洗函数"),
        (r"backend\publish_queue.py", "def _unique_path", "重名函数"),
        (r"backend\publish_queue.py", 'archived = self._archive(t, ok=True)', "回写调用"),
        (r"frontend\src\components\publish\SettingsPanel.tsx", "发布成功后重命名素材", "前端开关"),
    ]
    fail = []
    for rel, needle, tag in checks:
        s = io.open(os.path.join(LOCAL, rel), encoding="utf-8").read()
        ok = needle in s
        print(f"  {'OK ' if ok else 'BAD'} {tag}")
        if not ok:
            fail.append(tag)
    # 语法
    import ast
    for f in ("storage", "publish_queue"):
        ast.parse(io.open(os.path.join(LOCAL, "backend", f + ".py"), encoding="utf-8").read())
        print(f"  OK  AST {f}.py")
    if fail:
        raise SystemExit(f"[FAIL] 自校验未过: {fail}")
    print("\n[DONE] rev26 补丁全部应用")


if __name__ == "__main__":
    if "re" not in sys.modules:
        import re  # noqa: F401
    apply()
