# -*- coding: utf-8 -*-
"""rev23-fetcher: 作品抓取按视频号 finder_id 归属入库(account_id 仅记抓取来源)。"""
import io, sys

P = r"D:\SPHGJ\backend\post_fetcher.py"
s = io.open(P, encoding="utf-8").read()
orig = s

# 1) 导入 cover_key
old = 'logger = logging.getLogger("sphgj")\n'
new = 'from .storage import cover_key\n\nlogger = logging.getLogger("sphgj")\n'
assert s.count(old) == 1, "logger anchor"
s = s.replace(old, new, 1)

# 2) 头注释补充归属说明
old = "所有状态写 storage;请求量记 post_fetch_meta 账本(今日请求次数可见)。\n"
new = ("所有状态写 storage;请求量记 post_fetch_meta 账本(今日请求次数可见)。\n"
       "归属:作品主键是视频号 finder_id(不是登录卡片 id),封面落 data/covers/<finder_key>/。\n")
assert s.count(old) == 1, "header"
s = s.replace(old, new, 1)

# 3) finder_id 属性
old = '''    # ---------------- 封面 ----------------
    async def _ensure_covers(self, context, rows):
        """下载缺失/变更的封面(md5 判重,已有即零请求)。返回下载数。"""
        aid = self.account["id"]
        out_dir = os.path.join(self.covers_root, aid)'''
new = '''    @property
    def finder_id(self):
        """作品归属主键:视频号 ID。登录卡片 id 会变,视频号 ID 不会。"""
        return (self.account.get("_log_finder_id") or "").strip()

    # ---------------- 封面 ----------------
    async def _ensure_covers(self, context, rows):
        """下载缺失/变更的封面(md5 判重,已有即零请求)。返回下载数。"""
        aid = self.account["id"]
        key = cover_key(self.finder_id)      # 封面目录按视频号归属,换卡片不重下
        out_dir = os.path.join(self.covers_root, key)'''
assert s.count(old) == 1, "ensure_covers head"
s = s.replace(old, new, 1)

old = '''                r["cover_path"] = f"/media/covers/{aid}/{fname}"
                continue'''
new = '''                r["cover_path"] = f"/media/covers/{key}/{fname}"
                continue'''
assert s.count(old) == 1, "cover_path exists"
s = s.replace(old, new, 1)

old = '''                        r["cover_path"] = f"/media/covers/{aid}/{fname}"
                        return 1'''
new = '''                        r["cover_path"] = f"/media/covers/{key}/{fname}"
                        return 1'''
assert s.count(old) == 1, "cover_path new"
s = s.replace(old, new, 1)

# 4) refresh: 账本/早停/入库全部按 finder_id
old = '''        aid = self.account["id"]
        meta = self.storage.get_post_fetch_meta(aid)'''
new = '''        aid = self.account["id"]
        fid = self.finder_id
        if not fid:
            return {"error": "缺少视频号 ID(_log_finder_id),请先启动引擎抓取一次"}
        meta = self.storage.get_post_fetch_meta(fid)'''
assert s.count(old) == 1, "meta by finder"
s = s.replace(old, new, 1)

old = '''        fid = self.account.get("_log_finder_id", "")
        aidv = self.account.get("_aid", "")
        if not fid or not aidv:
            return {"error": "缺少 _aid/_log_finder_id,请先启动引擎抓取一次"}'''
new = '''        aidv = self.account.get("_aid", "")
        if not aidv:
            return {"error": "缺少 _aid,请先启动引擎抓取一次"}'''
assert s.count(old) == 1, "aidv check"
s = s.replace(old, new, 1)

old = '''                page_all_known = await asyncio.to_thread(
                    self.storage.posts_all_known, aid, [r["object_id"] for r in rows])
                # 封面判重 + 下载(先算 cover_path 再入库)
                covers += await self._ensure_covers(context, rows)
                await asyncio.to_thread(self.storage.upsert_posts, aid, rows)'''
new = '''                page_all_known = await asyncio.to_thread(
                    self.storage.posts_all_known, fid, [r["object_id"] for r in rows])
                # 封面判重 + 下载(先算 cover_path 再入库)
                covers += await self._ensure_covers(context, rows)
                # 归属 = fid;aid 只记「这批作品是哪个登录卡片抓的」,仅作追溯
                await asyncio.to_thread(self.storage.upsert_posts, fid, rows, aid)'''
assert s.count(old) == 1, "upsert by finder"
s = s.replace(old, new, 1)

old = '''                await asyncio.to_thread(self.storage.set_post_fetch_meta, aid, full_synced=1)'''
new = '''                await asyncio.to_thread(self.storage.set_post_fetch_meta, fid, account_id=aid, full_synced=1)'''
assert s.count(old) == 1, "set meta by finder"
s = s.replace(old, new, 1)

assert s != orig
io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = [
    ("导入 cover_key", "from .storage import cover_key" in s),
    ("finder_id 属性", "def finder_id(self)" in s),
    ("封面按视频号", 'out_dir = os.path.join(self.covers_root, key)' in s),
    ("入库按视频号", "self.storage.upsert_posts, fid, rows, aid" in s),
    ("早停按视频号", "self.storage.posts_all_known, fid," in s),
    ("账本按视频号", "self.storage.get_post_fetch_meta(fid)" in s),
    ("无封面按 aid", "/media/covers/{aid}/" not in s),
]
bad = [n for n, ok in chk if not ok]
print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
sys.exit(0 if not bad else 1)
