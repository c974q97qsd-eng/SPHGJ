"""评论抓取引擎:遍历视频 -> 增量抓评论 -> 存 SQLite。

优化(减少 comment_list 调用):
  1. post_list 返回 commentCount,=0 的视频直接跳过
  2. 本地存每视频上次 commentCount,没增加的跳过
  3. 评论按 commentId 去重(已存在的 upsert,不重复计数)

⚠️ 早停(优化 1/2)的两条硬约束 —— 2026-09-26 线上「好几天新增0评论」事故的教训:

  a) commentCount 字段缺失 ≠ 真的没有评论。用 `v.get("commentCount", 0) or 0`
     会把「字段没返回」和「接口请求失败」一起吞成 0;把这个 0 写进本地快照后,
     该视频从此走「没评论」分支永远跳过。所以字段缺失时【既不写快照也不跳过】。
  b) `prev == cc` 才跳过的前提是【本地真的抓到过评论】。仅凭 video_stats 里的
     count 不足以判定 —— 那个值可能正是被脏 0 污染的。故必须同时确认
     comments 表已有该视频的行(见 storage.get_video_fetch_state)。

rev52 分层扫描(用户 2026-10-28 定的规则):请求量的 88% 花在「每轮翻完全部作品只为
拿 commentCount」上 —— 8 个账号 6955 个作品,pageSize=20 时 348 页/轮,白天 10 分钟
一轮 = 41760 次/天,单轮串行节流耗时 10.1 分钟,已把 10 分钟的周期额度吃满(实际被拖到
13.5 分钟)。改法:
    - pageSize 20 -> 100(实测可翻页、无重复、无遗漏)
    - 分区(backend/video_zone.py):按播放数增长排序,前 30 热区、31~80 普通区、其余冷区
    - 每轮只取第 1 页(100 个作品),热区每轮查;普通区每 scan_normal_every 轮;
      冷区只在 scan_cold_every 轮的全量扫里查

  早停约束 a/b 在三个分区里【一律不变】—— 分区只决定「查不查这个视频」,
  不改变「怎么判断要不要发 comment_list」。这点是刻意的:分区是可回退的性能策略,
  早停正确性是历史事故换来的,不能混在一起改。

storage 调用一律走 _s() 放线程池(asyncio.to_thread),避免同步 SQLite 阻塞
uvicorn 事件循环(多 worker 并发写锁等待会卡死事件循环,导致 8712 不响应)。

返回 (扫描视频数, 新增评论数, 新评论列表[dict], 已删评论 id 列表, 本轮是否完整)
"""
import asyncio
import logging

from . import video_zone

logger = logging.getLogger("sphgj")


def _shape(finder_id, account_id, export_id, cmt):
    """把 API 原始评论对象整成前端友好的 dict(归属 = finder_id 视频号)。"""
    return {
        "finder_id": finder_id,
        "account_id": account_id,
        "export_id": export_id,
        "comment_id": cmt.get("commentId"),
        "nickname": cmt.get("commentNickname"),
        "content": cmt.get("commentContent"),
        "head_url": cmt.get("commentHeadurl"),
        "create_time": int(cmt.get("commentCreatetime", 0) or 0),
        "like_count": int(cmt.get("commentLikeCount", 0) or 0),
    }


class CommentFetcher:
    # 流式推送阈值:新评论缓冲累积到该条数即推送一次并清空。
    # 目的:高评论量账号(如直播中)一次扫描可能产生数千条新评论,全量堆在内存里等
    # fetch_all 结束才推送 -> 峰值内存 O(总评论数);分批推送后缓冲恒定在阈值规模,
    # 峰值降到 O(阈值)。200 条约 100KB,对推送开销与内存占用的折中取值。
    FLUSH_THRESHOLD = 200

    # rev52: 单页失败重试。errCode=-1 线上实测 0.5% 概率(2 号重扫后 20 分钟内 4 次),
    # 但它会让 _fetch_all_inner 直接 break,后面的作品全部漏扫,而且照样打
    # 「扫描N视频 新增0评论」—— 看起来这轮成功了。重试比漏扫便宜得多。
    RETRY_DELAYS = (0, 1.5, 4.0)

    # 登录态失效:重试没有意义(等几秒也不会自动恢复),直接交给回退路径。
    NO_RETRY_CODES = (300333, 300330)

    def __init__(self, api_client, storage, account_id, auto_reply=None, auto_commenter=None,
                 auto_delete=None, finder_getter=None, config=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_reply = auto_reply
        self.auto_commenter = auto_commenter
        self.auto_delete = auto_delete
        # 归属视频号:登录后从 profile 读到的 finder_id(随扫码/重登变化),每次现取
        self._finder_getter = finder_getter
        self.config = config if config is not None else {}
        self._round = 0          # 轮次计数(决定本轮是热区/普通区/全量)
        self._last_zone = None   # 上一次分区结果,供日志/状态展示

    @property
    def finder_id(self):
        """归属主键 = 视频号 finder_id。读不到(未登录/新卡片)时用 acct:<卡片> 兜底,
        之后 migrate_comments_to_finder 会把它换成真值,数据不会丢。"""
        try:
            fid = (self._finder_getter() if self._finder_getter else "") or ""
        except Exception:
            fid = ""
        fid = fid.strip()
        return fid or ("acct:" + self.account_id)

    # ==================== 请求重试(rev52) ====================

    @staticmethod
    def _err_code(resp):
        """业务错误码。0/None = 正常。"""
        if not isinstance(resp, dict):
            return -1
        for key in ("errCode", "ret"):
            v = resp.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.lstrip("-").isdigit():
                return int(v)
        return 0

    async def _call(self, fn, **kw):
        """读接口调用 + 有限重试。返回最后一个响应(调用方自行判定成败)。"""
        last = None
        for i, delay in enumerate(self.RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await fn(**kw)
            except Exception as e:
                last = {"__err": "exception:%s" % repr(e)[:80]}
            else:
                last = resp
                code = self._err_code(resp)
                if not (isinstance(resp, dict) and resp.get("__err")) and code == 0:
                    return resp
                if code in self.NO_RETRY_CODES:
                    break
            if i < len(self.RETRY_DELAYS) - 1:
                logger.info(f"[{self.account_id}] 请求失败 {last if not isinstance(last, dict) else list(last.items())[:3]},"
                            f"重试第 {i + 1} 次 args={list(kw)}")
        return last

    def _ok(self, resp):
        return (isinstance(resp, dict) and not resp.get("__err")
                and self._err_code(resp) == 0)

    async def _s(self, fn, *args):
        """storage 同步方法放线程池执行,避免 SQLite 阻塞事件循环。"""
        return await asyncio.to_thread(fn, *args)

    async def _flush(self, new_comments, deleted_ids, on_batch):
        """流式推送:把累积的新评论/已删 id 交给回调,成功后清空缓冲。

        异常时【不清空】——保留缓冲,由 fetch_all 的最终返回值补发,保证不丢评论。
        """
        if not on_batch or (not new_comments and not deleted_ids):
            return
        try:
            await on_batch(list(new_comments), list(deleted_ids))
        except Exception as e:
            logger.error(f"[{self.account_id}] 流式推送失败,保留缓冲由最终结果补发: {e}")
            return
        new_comments.clear()
        deleted_ids.clear()

    # ==================== 主流程 ====================

    async def fetch_all(self, max_videos=None, on_batch=None):
        """按分层策略抓一轮评论。

        返回 (扫描视频数, 新增评论数, 新评论列表, 已删评论 id 列表, 是否完整)。
        「是否完整」= 本轮有没有因为请求失败而提前中断(rev52 新增,原来静默中断看不出来)。
        """
        self._round += 1
        try:
            return await self._fetch_all_inner(max_videos=max_videos, on_batch=on_batch)
        finally:
            try:
                n = await self._s(self.storage.reconcile_own_comments, self.finder_id)
                if n:
                    logger.info(f"[{self.account_id}] 补登自己发出的评论 {n} 条")
            except Exception as e:
                logger.error(f"[{self.account_id}] 补登自己发出的评论失败: {e}")

    def _mode(self):
        """本轮扫描模式:full(全量) / normal(热区+普通区) / heat(只热区)。"""
        try:
            cold_every = int(self.config.get("scan_cold_every", 24) or 0)
            normal_every = int(self.config.get("scan_normal_every", 3) or 0)
        except (TypeError, ValueError):
            cold_every, normal_every = 24, 3
        full = cold_every > 0 and self._round % cold_every == 0
        if full:
            return "full"
        if normal_every > 0 and self._round % normal_every == 0:
            return "normal"
        return "heat"

    async def _collect_videos(self, full, max_videos=None):
        """拉取作品列表。非全量轮只取第 1 页(pageSize=100)就够 —— 分区的前 80 名一定在里面。

        返回 (videos, total_pages, complete)。
        """
        videos, last_buff, pages, complete = [], "", 0, True
        while True:
            resp = await self._call(self.api.fetch_video_list, last_buff=last_buff,
                                    only_unread=False)
            pages += 1
            if not self._ok(resp):
                # 重试也没救回来:标记本轮不完整,让上层打 WARNING
                logger.warning(f"[{self.account_id}] post_list 失败(第 {pages} 页, 已取 "
                               f"{len(videos)} 个作品),本轮标记为不完整: {resp}")
                complete = False
                break
            data = resp.get("data") or {}
            batch = data.get("list") or []
            if not batch:
                break
            videos.extend(batch)
            last_buff = data.get("lastBuff") or ""
            if not last_buff:
                break
            if not full:
                break                      # 非全量轮:第 1 页足够
            if max_videos and len(videos) >= max_videos:
                break
        return videos, pages, complete

    async def _fetch_all_inner(self, max_videos=None, on_batch=None):
        mode = self._mode()
        full = (mode == "full")
        include_normal = mode in ("full", "normal")

        videos, pages, complete = await self._collect_videos(full, max_videos=max_videos)
        if not videos:
            return 0, 0, [], [], complete

        # 播放数采样 + 3 日增长 -> 分区
        zone = await self._build_zones(videos)
        self._last_zone = zone
        pick = video_zone.wanted(zone["zones"], full=full, include_normal=include_normal)

        new_comments, deleted_ids = [], []
        total_new = 0
        checked = 0
        for v in videos:
            oid = v.get("objectId")
            if not oid or oid not in pick:
                continue
            # 评论数:必须区分「字段缺失」与「真的是 0」。
            # 用 `or 0` 兜底会把两者混同,而后续两个早停分支都吃这个值,
            # 一旦把缺失误当 0 写进快照就会永久跳过该视频(2026-09-26 事故)。
            raw_cc = v.get("commentCount")
            cc_missing = raw_cc is None
            try:
                cc = int(raw_cc or 0)
            except (TypeError, ValueError):
                cc_missing, cc = True, 0
            if cc_missing:
                # 字段没返回:本轮无法判断评论数,不写快照、不跳过,照样抓一次。
                # 代价 = 多一次 comment_list,但绝不会把视频锁死。
                logger.debug(f"[{self.account_id}] {oid} commentCount 缺失,强制抓取")
                st = None
            else:
                st = await self._s(self.storage.get_video_fetch_state, self.finder_id, oid)

            # 新视频检测(本地无记录)-> 自动评论+置顶
            is_new = (st is None) or (not st["seen"])
            if is_new and self.auto_commenter:
                await self._maybe_auto_comment(oid)

            if st is not None:
                # 优化1:接口明确说没评论 -> 跳过(记录快照)。set 内部有单调性保护,
                # 不会把已有的正数打回 0。
                if not cc:
                    await self._s(self.storage.set_video_comment_count, self.finder_id, oid, 0,
                                  self.account_id)
                    continue
                # 优化2:评论数没增加 -> 跳过。前提是【本地真抓到过评论】:
                # has_comments=False 说明历史快照可能是脏 0(从未抓过),
                # 此时必须强制抓一次,否则永远补不回来。
                prev = st["cc"]
                if prev is not None and prev == cc and st["has_comments"]:
                    continue

            # 抓评论
            checked += 1
            n, new_subs, del_ids, page_ok = await self._fetch_comments_for_video(
                oid, on_batch=on_batch)
            if not page_ok:
                complete = False
            total_new += n
            new_comments.extend(new_subs)
            deleted_ids.extend(del_ids)
            # 流式:缓冲达阈值即推送并清空(单视频内部也会 flush,这里兜住跨视频累积)
            if on_batch and len(new_comments) >= self.FLUSH_THRESHOLD:
                await self._flush(new_comments, deleted_ids, on_batch)
            if st is not None:
                # 抓到评论才更新为接口汇报值;没抓到(如接口临时失败)不落值,
                # 免得把「没抓到」写成「评论数=0」再次污染快照。
                if n or cc == 0:
                    await self._s(self.storage.set_video_comment_count, self.finder_id, oid, cc,
                                  self.account_id)
            if max_videos and checked >= max_videos:
                break

        self._log_round(mode, len(videos), checked, total_new, pages, zone, complete)
        return len(videos), total_new, new_comments, deleted_ids, complete

    async def _maybe_auto_comment(self, oid):
        """自动评论。达每日写上限后当日直接跳过,不再逐条撞上限。

        rev52: 原来每个新作品都调 try_comment -> post_comment 被上限挡回,
        线上一天打了 5574 条「写操作达每日上限」WARNING,把真正有用的日志淹掉。
        """
        if self.api is not None and hasattr(self.api, "write_blocked"):
            try:
                if self.api.write_blocked():
                    return
            except Exception:
                pass
        try:
            await self.auto_commenter.try_comment(oid)
        except Exception as e:
            logger.error(f"[{self.account_id}] 自动评论异常 {oid}: {e}")

    async def _build_zones(self, videos):
        """采样当日播放数 + 算 3 日增长 + 分区。失败一律降级,绝不阻断抓取。"""
        try:
            await self._s(self.storage.upsert_video_activity, self.finder_id, videos,
                          self.account_id)
        except Exception as e:
            logger.debug(f"[{self.account_id}] 写入播放数采样失败(不影响抓取): {e}")
        growth = {}
        try:
            days = int(self.config.get("zone_growth_days", 3) or 3)
            base = await self._s(self.storage.get_activity_baseline, self.finder_id, days)
            if base:
                for v in videos:
                    oid = v.get("objectId")
                    rc = v.get("readCount")
                    b = base.get(oid)
                    if oid and b is not None and rc is not None:
                        try:
                            growth[oid] = int(rc) - int(b)
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            logger.debug(f"[{self.account_id}] 读取播放数基线失败,降级为绝对值排序: {e}")
        try:
            heat = int(self.config.get("zone_heat_top", video_zone.HEAT_TOP) or video_zone.HEAT_TOP)
            normal = int(self.config.get("zone_normal_top", video_zone.NORMAL_TOP) or video_zone.NORMAL_TOP)
        except (TypeError, ValueError):
            heat, normal = video_zone.HEAT_TOP, video_zone.NORMAL_TOP
        return video_zone.classify(videos, growth, heat_top=heat, normal_top=normal)

    def _log_round(self, mode, total, checked, new_n, pages, zone, complete):
        """一轮结束的统一日志。保留「扫描N视频」前缀(日志分析脚本依赖)。"""
        tag = "" if complete else " ⚠️本轮不完整(有请求失败,部分作品未检查)"
        logger.info(
            f"[{self.account_id}] 扫描{total}视频(检查{checked}) 新增{new_n}评论 "
            f"[{video_zone.describe(zone, full=(mode == 'full'), include_normal=(mode != 'heat'))}"
            f" 页{pages}]{tag}")

    async def _fetch_comments_for_video(self, export_id, on_batch=None):
        """抓单个视频所有评论(分页 lastBuff)。

        返回 (新增数, 新评论列表, 已删评论 id 列表, 是否完整)。
        """
        new_count = 0
        new_list = []
        deleted_ids = []
        last_buff = ""
        while True:
            resp = await self._call(self.api.fetch_comments, export_id=export_id,
                                    last_buff=last_buff)
            if not self._ok(resp):
                logger.warning(f"[{self.account_id}] comment_list 失败 export={export_id}: {resp}")
                return new_count, new_list, deleted_ids, False
            data = resp.get("data") or {}
            comments = data.get("comment") or []
            for cmt in comments:
                cid = cmt.get("commentId")
                if not cid:
                    continue
                is_new = not (await self._s(self.storage.is_comment_exists, cid))
                await self._s(self.storage.upsert_comment, self.finder_id, export_id, cmt,
                              self.account_id)
                if is_new:
                    # 自动删除优先:命中关键字则删,不进新评论列表,不触发自动回复
                    deleted = False
                    if self.auto_delete:
                        try:
                            deleted = await self.auto_delete.delete_comment(cmt, export_id)
                        except Exception as e:
                            logger.error(f"[{self.account_id}] 自动删除异常 {cid}: {e}")
                    if deleted:
                        deleted_ids.append(cid)
                    else:
                        new_count += 1
                        new_list.append(_shape(self.finder_id, self.account_id, export_id, cmt))
                        # 流式:单视频内评论量大时(直播中)及时推送,避免缓冲膨胀
                        if on_batch and len(new_list) >= self.FLUSH_THRESHOLD:
                            await self._flush(new_list, deleted_ids, on_batch)
                        # 未删的新评论触发自动回复
                        if self.auto_reply:
                            try:
                                await self.auto_reply.reply_comment(cmt, export_id)
                            except Exception as e:
                                logger.error(f"[{self.account_id}] 自动回复异常 {cid}: {e}")
                # 二级评论(回复)
                for sub in cmt.get("levelTwoComment") or []:
                    sid = sub.get("commentId")
                    if not sid:
                        continue
                    is_new_sub = not (await self._s(self.storage.is_comment_exists, sid))
                    await self._s(self.storage.upsert_comment, self.finder_id, export_id, sub,
                                  self.account_id)
                    if is_new_sub:
                        deleted_sub = False
                        if self.auto_delete:
                            try:
                                deleted_sub = await self.auto_delete.delete_comment(sub, export_id)
                            except Exception as e:
                                logger.error(f"[{self.account_id}] 自动删除异常 {sid}: {e}")
                        if deleted_sub:
                            deleted_ids.append(sid)
                        else:
                            new_count += 1
                            new_list.append(_shape(self.finder_id, self.account_id, export_id, sub))
                            if on_batch and len(new_list) >= self.FLUSH_THRESHOLD:
                                await self._flush(new_list, deleted_ids, on_batch)
            last_buff = data.get("lastBuff") or ""
            if not last_buff or not comments:
                break
        return new_count, new_list, deleted_ids, True
