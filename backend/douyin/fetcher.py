"""抖音评论抓取引擎：作品列表 -> 分区 -> 加密 item_id -> 评论分页 -> 子回复 -> 入库。

与视频号那套（backend/comment_fetcher.py）**共享策略，不共享代码**：
两者接口形状差得太远（lastBuff vs cursor/max_cursor、exportId vs item_id 加密串、
errCode vs status_code + extra），硬合并会让每一行都要 if platform。

但下面这些用血换来的规矩，是**原样搬过来**的：

1. **commentCount 字段缺失 ≠ 真的没有评论**。抖音的作品列表里
   `statistics.comment_count` 缺失时既不能写快照也不能跳过，否则该作品会被
   永久锁死在「0 评论」（视频号 2026-09-26 事故同型）。
2. **「评论数没增加」这个早停的前提是本地真的抓到过评论**（has_comments）。
   光看 video_stats 里的数字不够 —— 那个值本身可能就是脏 0。
3. 请求失败不能当成功。抖音的坑更隐蔽：路径错时 `status_code` 也是 0，
   所以 status 判定一律走 api.judge()，绝不用 `status_code == 0`。
4. 子回复接口对**个别作品**会返回 `status_code=4「服务器打瞌睡了」`
   （实测换任何参数都一样，换作品就正常）。这属于服务端按作品拒绝，
   必须**降级跳过**并计入报告，不能因此判整轮失败，也不能把失败当「没有子回复」。

统计口径提醒（实测）：comment/list 的 `total_count` 只算一级评论，
作品 `statistics.comment_count` **含子回复**。所以两者只能判断「有没有变化」，
不能相等比较 —— 本模块全程没有做过相等比较。
"""
import asyncio
import logging
import time

from .. import platforms, video_zone
from . import api

logger = logging.getLogger("sphgj")


class DouyinCommentFetcher:
    #: 单页失败重试。抖音偶发 status_code=4/-1，重试比漏扫便宜得多。
    RETRY_DELAYS = (0, 1.5, 4.0)
    #: 重试无意义的错误：登录态死了、路径不匹配（重试一万次也一样）。
    NO_RETRY = (api.ERR_NOT_LOGGED_IN, api.ERR_URL_MISMATCH)

    def __init__(self, client, storage, account_id, owner_key, config=None):
        self.client = client
        self.storage = storage
        self.account_id = account_id
        self.owner = owner_key
        self.config = config or {}
        self._round = 0
        self._last_zone = None
        #: 本轮被服务端按作品拒绝子回复的作品（只做日志，不影响完成度）
        self.reply_rejected = {}

    # ---------------- 小工具 ----------------
    def _cfg_int(self, key, default):
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    async def _s(self, fn, *args, **kw):
        """storage 同步方法放线程池，别拿 SQLite 锁阻塞事件循环。"""
        return await asyncio.to_thread(fn, *args, **kw)

    async def _call(self, fn, **kw):
        """读接口 + 有限重试。返回最后一个信封（调用方只看 __ok）。"""
        last = None
        for i, delay in enumerate(self.RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                env = await fn(**kw)
            except Exception as e:
                env = {"__ok": False, "__err": "exception:%r" % (e,)}
            last = env
            if env.get("__ok"):
                return env
            if env.get("__err") in self.NO_RETRY:
                # 登录态死了 / 接口改版：再试只是白打请求
                break
            if i < len(self.RETRY_DELAYS) - 1:
                logger.info("[%s] 抖音请求失败(%s)，重试第 %d 次 %s"
                            % (self.account_id, env.get("__err"), i + 1, list(kw)))
                # 失败会触发客户端退避，测试/小账号上把退避清掉才走得快
                try:
                    self.client._consec_fail = 0
                    self.client._backoff_until = 0.0
                except Exception:
                    pass
        return last

    def _mode(self):
        """本轮模式：full（全量）/ normal（热区+普通区）/ heat（只热区）。"""
        cold_every = self._cfg_int("scan_cold_every", 24)
        normal_every = self._cfg_int("scan_normal_every", 3)
        full = cold_every > 0 and self._round % cold_every == 0
        if full:
            return "full"
        if normal_every > 0 and self._round % normal_every == 0:
            return "normal"
        return "heat"

    # ---------------- 主流程 ----------------
    async def fetch_all(self, max_works=None):
        """抓一轮。返回报告 dict（不抛异常 —— 抓取失败只是报告里的 complete=False）。"""
        self._round += 1
        t0 = time.time()
        mode = self._mode()
        full = (mode == "full")

        works, list_ok = await self.client.fetch_all_works(
            max_pages=self._cfg_int("dy_work_pages", 40),
            count=self._cfg_int("dy_work_page_size", 12))
        if not works:
            return self._report(mode, 0, 0, [], list_ok, {}, t0,
                                note="作品列表为空或获取失败")

        zone = await self._build_zones(works)
        self._last_zone = zone
        pick = video_zone.wanted(zone["zones"], full=full,
                                 include_normal=(mode in ("full", "normal")))

        # ---- 早停筛选（三条规矩见文件顶部）----
        todo, skipped = [], 0
        for w in works:
            oid = w["aweme_id"]
            if not oid or oid not in pick:
                continue
            raw_cc = w.get("comment_count")
            cc_missing = raw_cc is None
            try:
                cc = int(raw_cc or 0)
            except (TypeError, ValueError):
                cc_missing, cc = True, 0
            if cc_missing:
                # 字段没返回：本轮无法判断，不写快照、不跳过，照样抓一次
                todo.append((w, None))
                continue
            st = await self._s(self.storage.get_video_fetch_state, self.owner, oid)
            if st["seen"]:
                if not cc:
                    # 接口明确说没评论 -> 记快照后跳过
                    await self._s(self.storage.set_video_comment_count, self.owner, oid, 0,
                                  self.account_id, platform="dy")
                    skipped += 1
                    continue
                if st["cc"] is not None and st["cc"] == cc and st["has_comments"]:
                    skipped += 1
                    continue
            todo.append((w, st))
            if max_works and len(todo) >= max_works:
                break

        if not todo:
            return self._report(mode, len(works), 0, [], list_ok, zone, t0,
                                skipped=skipped, note="本轮无需要检查的作品")

        # ---- 加密 item_id 映射（只对真要抓的作品翻页，省请求）----
        mapping = await self.client.map_item_ids(
            [w["aweme_id"] for w, _ in todo],
            max_pages=self._cfg_int("dy_item_pages", 30),
            count=self._cfg_int("dy_item_page_size", 20))
        unmapped = [w["aweme_id"] for w, _ in todo if w["aweme_id"] not in mapping]
        if unmapped:
            logger.info("[%s] 抖音有 %d 个作品拿不到加密 item_id，本轮跳过（下轮再试）: %s"
                        % (self.account_id, len(unmapped), ",".join(unmapped[:3])))

        # ---- 逐作品抓评论 ----
        new_comments, complete = [], list_ok
        checked = 0
        reply_rejected = 0
        max_pages = self._cfg_int("dy_comment_pages", 20)
        for w, st in todo:
            oid = w["aweme_id"]
            enc = mapping.get(oid)
            if not enc:
                continue
            checked += 1
            n, items, ok, rej = await self._fetch_one(oid, enc, max_pages=max_pages)
            new_comments.extend(items)
            reply_rejected += rej
            if not ok:
                complete = False
            # 抓到评论才更新快照；没抓到（接口失败）不落值，免得把「没抓到」写成 0。
            # ⚠️ 而且**字段缺失时一律不写** —— 单测直接抓到过这个 bug：cc 为 None 却因为
            # n>0 走了写入分支，`int(None or 0)` 把快照写成 0，下一轮就按「没评论」永久跳过。
            raw_cc = w.get("comment_count")
            if raw_cc is not None:
                try:
                    cc_val = int(raw_cc)
                except (TypeError, ValueError):
                    cc_val = None
                if cc_val is not None and (n or cc_val == 0):
                    await self._s(self.storage.set_video_comment_count, self.owner, oid,
                                  cc_val, self.account_id, platform="dy")

        if reply_rejected:
            logger.info("[%s] 抖音有 %d 组子回复被服务端按作品拒绝(status_code=4)，已降级跳过"
                        % (self.account_id, reply_rejected))
        rep = self._report(mode, len(works), checked, new_comments, complete, zone, t0,
                           skipped=skipped, unmapped=len(unmapped))
        rep["reply_rejected"] = reply_rejected
        return rep

    async def _fetch_one(self, aweme_id, item_id, max_pages=20):
        """抓一个作品的全部评论 + 子回复。

        返回 (新增数, 新评论列表, 是否完整, 子回复被拒次数)。
        """
        new_count, new_list, deleted_ids = 0, [], []
        cursor, pages, complete = 0, 0, True
        rejected = 0
        seen_cursors = set()
        while pages < max_pages:
            env = await self._call(self.client.fetch_comments, item_id=item_id,
                                   cursor=cursor, count=self._cfg_int("dy_comment_page_size", 20))
            pages += 1
            if not env.get("__ok"):
                logger.warning("[%s] 抖音 comment/list 失败 aweme=%s err=%s"
                               % (self.account_id, aweme_id, env.get("__err")))
                complete = False
                break
            rows = env.get("comments") or []
            for c in rows:
                cid = c.get("comment_id")
                if not cid:
                    continue
                is_new = not (await self._s(self.storage.is_comment_exists, cid))
                await self._s(self.storage.upsert_comment, self.owner, aweme_id,
                              api.to_storage_comment(c), self.account_id, platform="dy")
                if is_new:
                    new_count += 1
                    new_list.append(self._shape(aweme_id, c))
            # 子回复：一级评论 reply_count>0 才去翻，失败降级
            for c in rows:
                if int(c.get("reply_count") or 0) <= 0:
                    continue
                n2, ok2 = await self._fetch_replies(c, item_id, aweme_id)
                if not ok2:
                    rejected += 1
                    continue
                for sub in n2:
                    new_count += 1
                    new_list.append(self._shape(aweme_id, sub))
            if not env.get("has_more") or not rows:
                break
            nxt = env.get("cursor")
            if nxt in (None, "") or nxt in seen_cursors or nxt == cursor:
                # 游标不前进 = 到底了或参数被忽略，必须停，否则死循环
                break
            seen_cursors.add(cursor)
            cursor = nxt
        return new_count, new_list, complete, rejected

    async def _fetch_replies(self, comment, item_id, aweme_id):
        """抓一条评论下的子回复。返回 (新增列表, 是否成功)。

        🚨 item_id 是**加密串**（只用于发请求）；aweme_id 才是**明文**（用于入库）。
        两者绝不能混：export_id 列的语义是明文 aweme_id，存成加密串以后
        reply()/delete_comment() 再拿它去查映射表必然查不到，
        而且报错会伪装成「拿不到加密 item_id」，极难定位（踩过一次）。
        """
        try:
            env = await self._call(self.client.fetch_replies, comment_id=comment.get("comment_id"),
                                   item_id=item_id, cursor=0, count=20)
        except Exception as e:
            logger.info("[%s] 抖音 reply/list 异常 %s: %r"
                        % (self.account_id, comment.get("comment_id", "")[:12], e))
            return [], False
        if not env.get("__ok"):
            # status_code=4 = 服务端按作品拒绝（实测换参数无效），降级跳过
            return [], False
        out = []
        for sub in env.get("comments") or []:
            cid = sub.get("comment_id")
            if not cid:
                continue
            if not (await self._s(self.storage.is_comment_exists, cid)):
                await self._s(self.storage.upsert_comment, self.owner, aweme_id,
                              api.to_storage_comment(sub), self.account_id, platform="dy")
                out.append(sub)
        return out, True

    async def _build_zones(self, works):
        """采样播放数 + 3 日增长 + 分区。任何一步失败都降级，绝不阻断抓取。"""
        adapted = [{"objectId": w["aweme_id"], "readCount": w.get("read_count"),
                    "likeCount": w.get("like_count"), "commentCount": w.get("comment_count")}
                   for w in works]
        try:
            await self._s(self.storage.upsert_video_activity, self.owner, adapted,
                          self.account_id, platform="dy")
        except Exception as e:
            logger.debug("[%s] 抖音写入播放采样失败(不影响抓取): %r" % (self.account_id, e))
        growth = {}
        try:
            days = self._cfg_int("zone_growth_days", 3)
            base = await self._s(self.storage.get_activity_baseline, self.owner, days)
            if base:
                for w in works:
                    oid, rc = w["aweme_id"], w.get("read_count")
                    b = base.get(oid)
                    if oid and b is not None and rc is not None:
                        try:
                            growth[oid] = int(rc) - int(b)
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.debug("[%s] 抖音读播放基线失败,降级为绝对值排序: %r" % (self.account_id, e))
        heat = self._cfg_int("zone_heat_top", video_zone.HEAT_TOP)
        normal = self._cfg_int("zone_normal_top", video_zone.NORMAL_TOP)
        return video_zone.classify(adapted, growth, heat_top=heat, normal_top=normal)

    @staticmethod
    def _shape(aweme_id, c):
        """给前端/推送用的评论结构（对齐视频号那套字段名，前端可复用同一份渲染）。"""
        return {
            "finder_id": "",   # 由调用方补（保持与视频号同构，见 manager）
            "export_id": aweme_id,
            "comment_id": c.get("comment_id"),
            "nickname": c.get("nickname"),
            "content": c.get("text"),
            "head_url": c.get("head_url"),
            "create_time": int(c.get("create_time") or 0),
            "like_count": int(c.get("like_count") or 0),
            "level": int(c.get("level") or 1),
            "reply_count": int(c.get("reply_count") or 0),
            "is_author": bool(c.get("is_author")),
        }

    def _report(self, mode, total, checked, new_comments, complete, zone, t0,
                skipped=0, unmapped=0, note=""):
        n = len(new_comments)
        tag = "" if complete else " ⚠️本轮不完整(有请求失败,部分作品未检查)"
        logger.info("[%s] 抖音 扫描%d作品(检查%d) 新增%d评论 %s 跳过%d%s"
                    % (self.account_id, total, checked, n,
                       video_zone.describe(zone or {}, full=(mode == "full"),
                                           include_normal=(mode != "heat")),
                       skipped, tag))
        return {
            "platform": "dy",
            "account_id": self.account_id,
            "owner": self.owner,
            "mode": mode,
            "works": total,
            "checked": checked,
            "skipped": skipped,
            "unmapped": unmapped,
            "new": n,
            "new_comments": new_comments,
            "complete": complete,
            "zone": {"basis": (zone or {}).get("basis"), "counts": (zone or {}).get("counts")},
            "seconds": round(time.time() - t0, 1),
            "note": note,
        }
