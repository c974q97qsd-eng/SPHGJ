# -*- coding: utf-8 -*-
"""rev24 补丁 2/4:运行时归属改按视频号(finder_id)。

CommentFetcher / AutoReply / AutoDelete / AutoCommenter 都通过 finder_getter
实时取「当前登录的视频号」(登录后才有,_log_finder_id 会随扫码/重登变化),
写库一律用 finder_id;account_id 降级为「抓取来源卡片」。
"""
import io
import sys

ROOT = r"D:\SPHGJ\backend"
PATCHES = {"comment_fetcher.py": [], "auto_reply.py": [], "auto_delete.py": [],
           "auto_comment.py": [], "account_manager.py": []}


def p(f, tag, old, new):
    PATCHES[f].append((tag, old, new))


# ============================================================ comment_fetcher.py
F = "comment_fetcher.py"
p(F, "init", '''    def __init__(self, api_client, storage, account_id, auto_reply=None, auto_commenter=None, auto_delete=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_reply = auto_reply
        self.auto_commenter = auto_commenter
        self.auto_delete = auto_delete''',
  '''    def __init__(self, api_client, storage, account_id, auto_reply=None, auto_commenter=None,
                 auto_delete=None, finder_getter=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_reply = auto_reply
        self.auto_commenter = auto_commenter
        self.auto_delete = auto_delete
        # 归属视频号:登录后从 profile 读到的 finder_id(随扫码/重登变化),每次现取
        self._finder_getter = finder_getter

    @property
    def finder_id(self):
        """归属主键 = 视频号 finder_id。读不到(未登录/新卡片)时用 acct:<卡片> 兜底,
        之后 migrate_comments_to_finder 会把它换成真值,数据不会丢。"""
        try:
            fid = (self._finder_getter() if self._finder_getter else "") or ""
        except Exception:
            fid = ""
        fid = fid.strip()
        return fid or ("acct:" + self.account_id)''')

p(F, "reconcile", '''                n = await self._s(self.storage.reconcile_own_comments, self.account_id)''',
  '''                n = await self._s(self.storage.reconcile_own_comments, self.finder_id)''')

p(F, "state", '''                    st = await self._s(self.storage.get_video_fetch_state, self.account_id, oid)''',
  '''                    st = await self._s(self.storage.get_video_fetch_state, self.finder_id, oid)''')

p(F, "set0", '''                        await self._s(self.storage.set_video_comment_count, self.account_id, oid, 0)''',
  '''                        await self._s(self.storage.set_video_comment_count, self.finder_id, oid, 0,
                                       self.account_id)''')

p(F, "setcc", '''                        await self._s(self.storage.set_video_comment_count, self.account_id, oid, cc)''',
  '''                        await self._s(self.storage.set_video_comment_count, self.finder_id, oid, cc,
                                       self.account_id)''')

p(F, "upsert1", '''                await self._s(self.storage.upsert_comment, self.account_id, export_id, cmt)''',
  '''                await self._s(self.storage.upsert_comment, self.finder_id, export_id, cmt,
                              self.account_id)''')

p(F, "upsert2", '''                    await self._s(self.storage.upsert_comment, self.account_id, export_id, sub)''',
  '''                    await self._s(self.storage.upsert_comment, self.finder_id, export_id, sub,
                                  self.account_id)''')

# ============================================================ auto_reply.py
F = "auto_reply.py"
p(F, "init", '''    def __init__(self, api_client, storage, account_id, auto_config):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_config = auto_config or {}''',
  '''    def __init__(self, api_client, storage, account_id, auto_config, finder_getter=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_config = auto_config or {}
        self._finder_getter = finder_getter

    @property
    def finder_id(self):
        """归属主键 = 视频号 finder_id(取不到用 acct:<卡片> 兜底)。"""
        try:
            fid = (self._finder_getter() if self._finder_getter else "") or ""
        except Exception:
            fid = ""
        fid = fid.strip()
        return fid or ("acct:" + self.account_id)''')

p(F, "mark", '''                    await asyncio.to_thread(self.storage.mark_own_comment,
                                            self.account_id, new_cid, "auto_reply",
                                            content=reply_text, export_id=export_id)''',
  '''                    await asyncio.to_thread(self.storage.mark_own_comment,
                                            self.finder_id, new_cid, "auto_reply",
                                            content=reply_text, export_id=export_id,
                                            account_id=self.account_id)''')

p(F, "pending", '''                    await asyncio.to_thread(self.storage.mark_own_comment_pending,
                                            self.account_id, export_id, reply_text, "auto_reply")''',
  '''                    await asyncio.to_thread(self.storage.mark_own_comment_pending,
                                            self.finder_id, export_id, reply_text, "auto_reply",
                                            self.account_id)''')

# ============================================================ auto_delete.py
F = "auto_delete.py"
p(F, "init", '''    def __init__(self, api_client, storage, account_id, auto_config):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.update_config(auto_config or {})''',
  '''    def __init__(self, api_client, storage, account_id, auto_config, finder_getter=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self._finder_getter = finder_getter
        self.update_config(auto_config or {})

    @property
    def finder_id(self):
        """归属主键 = 视频号 finder_id(取不到用 acct:<卡片> 兜底)。"""
        try:
            fid = (self._finder_getter() if self._finder_getter else "") or ""
        except Exception:
            fid = ""
        fid = fid.strip()
        return fid or ("acct:" + self.account_id)''')

p(F, "log", '''                await asyncio.to_thread(self.storage.log_delete,
                                        self.account_id, cid, nickname, content, kw, export_id)''',
  '''                await asyncio.to_thread(self.storage.log_delete,
                                        self.finder_id, cid, nickname, content, kw, export_id,
                                        self.account_id)''')

# ============================================================ auto_comment.py
F = "auto_comment.py"
p(F, "init", '''    def __init__(self, api_client, storage, account):
        self.api = api_client
        self.storage = storage
        self.account = account''',
  '''    def __init__(self, api_client, storage, account):
        self.api = api_client
        self.storage = storage
        self.account = account

    @property
    def finder_id(self):
        """归属主键 = 视频号 finder_id(取不到用 acct:<卡片> 兜底)。"""
        a = self.account or {}
        fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip()
        return fid or ("acct:" + a.get("id", "unknown"))''')

p(F, "is", '''        if self.storage.is_auto_commented(self.account["id"], export_id):''',
  '''        if self.storage.is_auto_commented(self.finder_id, export_id):''')

p(F, "set1", '''                self.storage.set_auto_commented(self.account["id"], export_id, "")''',
  '''                self.storage.set_auto_commented(self.finder_id, export_id, "",
                                                self.account["id"])''')

p(F, "pending", '''                    self.storage.mark_own_comment_pending(self.account["id"], export_id, content, "auto_comment")''',
  '''                    self.storage.mark_own_comment_pending(self.finder_id, export_id, content,
                                                          "auto_comment", self.account["id"])''')

p(F, "set2", '''            self.storage.set_auto_commented(self.account["id"], export_id, comment_id)''',
  '''            self.storage.set_auto_commented(self.finder_id, export_id, comment_id, self.account["id"])''')

p(F, "mark", '''                self.storage.mark_own_comment(self.account["id"], comment_id, "auto_comment",
                                              content=content, export_id=export_id)''',
  '''                self.storage.mark_own_comment(self.finder_id, comment_id, "auto_comment",
                                              content=content, export_id=export_id,
                                              account_id=self.account["id"])''')

# ============================================================ account_manager.py
F = "account_manager.py"
p(F, "wire", '''        self.auto_reply = AutoReply(None, self.storage, self.account["id"],
                                    self.config.get("auto_reply") or {})
        self.auto_commenter = AutoCommenter(None, self.storage, self.account)
        self.auto_delete = AutoDelete(None, self.storage, self.account["id"],
                                      self.config.get("auto_delete") or {})
        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete)''',
  '''        # 归属视频号 getter:每次现取(登录/重登后 _log_finder_id 会变),
        # 评论、自动回复、自动删除、自动评论统一按视频号归属,不再按登录卡片
        _fid = (lambda: (self.account.get("_log_finder_id")
                         or self.account.get("locked_finder_id") or ""))
        self.auto_reply = AutoReply(None, self.storage, self.account["id"],
                                    self.config.get("auto_reply") or {}, finder_getter=_fid)
        self.auto_commenter = AutoCommenter(None, self.storage, self.account)
        self.auto_delete = AutoDelete(None, self.storage, self.account["id"],
                                      self.config.get("auto_delete") or {}, finder_getter=_fid)
        self.fetcher = CommentFetcher(None, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete,
                                      finder_getter=_fid)''')

# ============================================================ apply
fail = []
for fname, items in PATCHES.items():
    path = f"{ROOT}\\{fname}"
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in items:
        n = s.count(old)
        if n != 1:
            fail.append(f"{fname}:{tag} 命中 {n} 次")
            continue
        s = s.replace(old, new, 1)
    if not fail:
        io.open(path, "w", encoding="utf-8", newline="").write(s)
if fail:
    print("PATCH FAIL")
    for f in fail:
        print("  -", f)
    sys.exit(1)

# ============================================================ selfcheck
bad = []
s = io.open(f"{ROOT}\\comment_fetcher.py", encoding="utf-8").read()
if "def finder_id" not in s or s.count("self.finder_id") < 6:
    bad.append("fetcher finder_id")
if "upsert_comment, self.finder_id" not in s:
    bad.append("fetcher upsert")
s = io.open(f"{ROOT}\\auto_reply.py", encoding="utf-8").read()
if "def finder_id" not in s or "mark_own_comment_pending(self.finder_id" not in s:
    bad.append("auto_reply")
s = io.open(f"{ROOT}\\auto_delete.py", encoding="utf-8").read()
if "def finder_id" not in s or "log_delete," not in s:
    bad.append("auto_delete")
s = io.open(f"{ROOT}\\auto_comment.py", encoding="utf-8").read()
if "def finder_id" not in s or "is_auto_commented(self.finder_id" not in s:
    bad.append("auto_comment")
s = io.open(f"{ROOT}\\account_manager.py", encoding="utf-8").read()
if "finder_getter=_fid" not in s or s.count("finder_getter=_fid") != 3:
    bad.append("account_manager 注入")
print("SELFCHECK", "PASS" if not bad else "FAIL: " + ", ".join(bad))
sys.exit(0 if not bad else 2)
