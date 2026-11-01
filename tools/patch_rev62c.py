# -*- coding: utf-8 -*-
"""rev62c:自动评论加「只评论新发布作品」闸门。

背景(2026-11-02 实测):
  _is_new 的判据是「本地没有该作品的抓取快照」(video_stats 无行),不是「刚发布」。
  uspoloassn7 有 634 条作品、只自动评论过 90 条,最新作品停在 09-23,
  也就是说库里躺着 500+ 条从未评论过的老作品。
  这些老作品在写操作全失败时(no_page)只是每 10 分钟报一次警,不会造成可见后果;
  一旦写通道修好,一轮最多会把 100 条营销文案砸到几个月前的视频上 ——
  这是账号风控和口碑都受不了的。所以补一条时间闸门:
  作品发布超过 auto_comment_max_age_days(默认 7 天)就不再自动评论。
  account 级可覆盖(account.auto_comment_max_age_days),设 0/负数 = 关闭闸门。
"""
import io

BASE = r"D:\SPHGJ\backend"

# ---------------- auto_comment.py ----------------
P1 = BASE + r"\auto_comment.py"
s1 = io.open(P1, encoding="utf-8").read()
o1 = s1

PATCH1 = [
    (
        '''import logging
logger = logging.getLogger("sphgj")''',
        '''import logging
import time

logger = logging.getLogger("sphgj")''',
        "import_time",
    ),
    (
        '''    def is_enabled(self):
        return bool(self.account.get("auto_comment_enabled")) and bool(self.account.get("auto_comment_content"))''',
        '''    def is_enabled(self):
        return bool(self.account.get("auto_comment_enabled")) and bool(self.account.get("auto_comment_content"))

    def max_age_days(self):
        """只评论「新发布」的作品:发布超过 N 天的不评论。0/负数 = 关闭闸门(老行为)。

        rev62c: 触发上层的 is_new 是「本地没抓过这条作品」,不是「刚发布」。
        浏览器收起的账号(非直播常态)写操作原来全失败,积压了大量从没评论过的
        老作品;写通道修好后会在同一轮里逐条补评论 —— 一轮最多 100 条。
        """
        v = self.account.get("auto_comment_max_age_days")
        if v is None:
            cfg = getattr(self.api, "config", None) or {}
            v = cfg.get("auto_comment_max_age_days", 7)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 7''',
        "max_age_days",
    ),
    (
        '''    async def try_comment(self, export_id):
        """新视频:发评论 + 置顶(未发过才发)。返回是否成功。"""
        if not self.is_enabled():
            return False
        if self.storage.is_auto_commented(self.finder_id, export_id):
            return False''',
        '''    async def try_comment(self, export_id, create_time=None):
        """新视频:发评论 + 置顶(未发过才发)。返回是否成功。

        create_time 是该作品的发布时间(unix 秒,接口 createTime);给了就按
        auto_comment_max_age_days 判断是不是「新发布」。
        """
        if not self.is_enabled():
            return False
        if self.storage.is_auto_commented(self.finder_id, export_id):
            return False
        lim = self.max_age_days()
        if lim > 0 and create_time:
            try:
                age_days = (time.time() - int(create_time)) / 86400.0
            except (TypeError, ValueError):
                age_days = 0.0
            if age_days > lim:
                logger.debug("[%s] 跳过自动评论:作品已发布 %.1f 天 > %d 天 %s",
                             self.account["id"], age_days, lim, export_id)
                return False''',
        "try_comment_gate",
    ),
]

for old, new, tag in PATCH1:
    assert s1.count(old) == 1, "auto_comment 锚点问题: %s (count=%d)" % (tag, s1.count(old))
    s1 = s1.replace(old, new, 1)
io.open(P1, "w", encoding="utf-8", newline="").write(s1)

# ---------------- comment_fetcher.py ----------------
P2 = BASE + r"\comment_fetcher.py"
s2 = io.open(P2, encoding="utf-8").read()
o2 = s2

PATCH2 = [
    (
        """            is_new = (st is None) or (not st["seen"])
            if is_new and self.auto_commenter:
                await self._maybe_auto_comment(oid)""",
        """            is_new = (st is None) or (not st["seen"])
            if is_new and self.auto_commenter:
                # rev62c: 把发布时间传下去,由 AutoCommenter 判断是不是「新发布」。
                await self._maybe_auto_comment(oid, v.get("createTime"))""",
        "pass_create_time",
    ),
    (
        '''    async def _maybe_auto_comment(self, oid):
        """自动评论。达每日写上限后当日直接跳过,不再逐条撞上限。''',
        '''    async def _maybe_auto_comment(self, oid, create_time=None):
        """自动评论。达每日写上限后当日直接跳过,不再逐条撞上限。

        create_time: 作品发布时间(unix 秒)。AutoCommenter 用它挡掉老作品。
        ''',
        "maybe_sig",
    ),
    (
        """        try:
            await self.auto_commenter.try_comment(oid)""",
        """        try:
            await self.auto_commenter.try_comment(oid, create_time)""",
        "call_pass",
    ),
]

for old, new, tag in PATCH2:
    assert s2.count(old) == 1, "comment_fetcher 锚点问题: %s (count=%d)" % (tag, s2.count(old))
    s2 = s2.replace(old, new, 1)
io.open(P2, "w", encoding="utf-8", newline="").write(s2)

ok = True
for needle, where in [("def max_age_days(self)", s1), ("try_comment(oid, create_time)", s2),
                      ('v.get("createTime")', s2), ("import time", s1)]:
    hit = needle in where
    print(("  OK   " if hit else "  FAIL ") + needle)
    ok = ok and hit
print("SELFCHECK", "PASS" if ok else "FAIL",
      "| auto_comment %d->%d | comment_fetcher %d->%d" % (len(o1), len(s1), len(o2), len(s2)))
raise SystemExit(0 if ok else 1)
