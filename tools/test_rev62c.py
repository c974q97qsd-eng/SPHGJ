# -*- coding: utf-8 -*-
"""rev62c 单测:自动评论的时间闸门(老作品不评论,新作品照发,可关)。"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.auto_comment import AutoCommenter

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  << " + str(extra)) if not cond else ""))


class StubApi:
    def __init__(self, config=None):
        self.posts = []
        self.config = config or {}

    async def post_comment(self, export_id, content):
        self.posts.append(export_id)
        return {"errCode": 0, "data": {"comment": {"commentId": "C1"}}}

    async def pin_comment(self, export_id, comment_id, op_type=1):
        return {"errCode": 0}

    def failed(self, resp):
        return bool(resp.get("__err")) or resp.get("errCode") not in (0, None)


class StubStorage:
    def __init__(self):
        self.done = set()

    def is_auto_commented(self, fid, export_id):
        return export_id in self.done

    def set_auto_commented(self, fid, export_id, comment_id, account_id):
        self.done.add(export_id)

    def mark_own_comment_pending(self, *a, **k):
        pass

    def mark_own_comment(self, *a, **k):
        pass


ACC = {"id": "uspoloassn7", "_log_finder_id": "FID7",
       "auto_comment_enabled": True, "auto_comment_content": "点头像，进入直播间领取优惠券下单！"}


def mk(acc=None, config=None):
    a = dict(ACC)
    a.update(acc or {})
    api = StubApi(config)
    return AutoCommenter(api, StubStorage(), a), api


def run(c):
    return asyncio.run(c)


now = int(time.time())
OLD = now - 60 * 86400      # 60 天前
NEW = now - 3600            # 1 小时前

# 1. 老作品(60 天)不评论
ac, api = mk()
r = run(ac.try_comment("export/OLD", OLD))
check("1 老作品(60天)不自动评论", r is False and api.posts == [], api.posts)

# 2. 新作品照发
ac, api = mk()
r = run(ac.try_comment("export/NEW", NEW))
check("2 新作品(1小时)正常评论", r is True and api.posts == ["export/NEW"], (r, api.posts))

# 3. 没传 createTime 时保持老行为(照发)
ac, api = mk()
r = run(ac.try_comment("export/NOTS"))
check("3 未传 createTime 保持老行为", r is True and api.posts == ["export/NOTS"], (r, api.posts))

# 4. 边界:刚好 6 天(在 7 天闸门内)照发;8 天挡掉
ac, api = mk()
run(ac.try_comment("export/D6", now - 6 * 86400))
run(ac.try_comment("export/D8", now - 8 * 86400))
check("4 6 天发 / 8 天不发", api.posts == ["export/D6"], api.posts)

# 5. account 级关闸门(max_age_days=0)后老作品照发
ac, api = mk({"auto_comment_max_age_days": 0})
r = run(ac.try_comment("export/OLD2", OLD))
check("5 关闸门后老作品照发", r is True and api.posts == ["export/OLD2"], (r, api.posts))

# 6. 全局配置覆盖默认值(全局 30 天 -> 60 天前照挡,20 天前放行)
ac, api = mk(config={"auto_comment_max_age_days": 30})
run(ac.try_comment("export/D20", now - 20 * 86400))
run(ac.try_comment("export/D60", OLD))
check("6 全局配置生效(20天发/60天不发)", api.posts == ["export/D20"], api.posts)

# 7. 已评论过的不重复
ac, api = mk()
run(ac.try_comment("export/NEW2", NEW))
run(ac.try_comment("export/NEW2", NEW))
check("7 同一条只评论一次", api.posts == ["export/NEW2"], api.posts)

print()
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
raise SystemExit(0 if not FAIL else 1)
