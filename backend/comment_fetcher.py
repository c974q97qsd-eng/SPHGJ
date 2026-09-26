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

storage 调用一律走 _s() 放线程池(asyncio.to_thread),避免同步 SQLite 阻塞
uvicorn 事件循环(多 worker 并发写锁等待会卡死事件循环,导致 8712 不响应)。

返回 (扫描视频数, 新增评论数, 新评论列表[dict])--新评论列表供 WS 实时推送。
"""
import asyncio
import logging
logger = logging.getLogger("sphgj")


def _shape(account_id, export_id, cmt):
    """把 API 原始评论对象整成前端友好的 dict。"""
    return {
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

    def __init__(self, api_client, storage, account_id, auto_reply=None, auto_commenter=None, auto_delete=None):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_reply = auto_reply
        self.auto_commenter = auto_commenter
        self.auto_delete = auto_delete

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

    async def fetch_all(self, max_videos=None, on_batch=None):
        """遍历所有视频,增量抓评论。返回 (扫描视频数, 新增评论数, 新评论列表, 已删评论 id 列表)。

        on_batch: 可选 async 回调 fn(comments, deleted_ids)。给定时启用【流式推送】——
                  缓冲累积到 FLUSH_THRESHOLD 条即推送并清空,避免高评论量账号在内存堆积
                  全量新评论。不传则保持原有行为(全部抓完一次性返回)。
                  注意:返回的"新评论列表"仅含【未推送完的剩余部分】,新增数则是完整总数。

        抓完一轮后统一补登「自己发出的评论」:自动评论/回复发出但当时没拿到 comment_id 的,
        此时评论已被抓进库,按 export_id+内容精确匹配补上真实 id(见 storage.reconcile_own_comments)。
        """
        try:
            return await self._fetch_all_inner(max_videos=max_videos, on_batch=on_batch)
        finally:
            try:
                n = await self._s(self.storage.reconcile_own_comments, self.account_id)
                if n:
                    logger.info(f"[{self.account_id}] 补登自己发出的评论 {n} 条")
            except Exception as e:
                logger.error(f"[{self.account_id}] 补登自己发出的评论失败: {e}")

    async def _fetch_all_inner(self, max_videos=None, on_batch=None):
        scanned = 0
        new_comments = []   # 待推送缓冲(流式模式下会周期性清空)
        deleted_ids = []
        total_new = 0       # 新增总数(独立于缓冲:流式清空后计数仍准确)
        last_buff = ""
        while True:
            resp = await self.api.fetch_video_list(last_buff=last_buff, only_unread=False)
            if not resp or resp.get("__err"):
                logger.warning(f"[{self.account_id}] post_list 失败: {resp}")
                break
            data = resp.get("data") or {}
            videos = data.get("list") or []
            if not videos:
                break
            for v in videos:
                oid = v.get("objectId")
                if not oid:
                    continue
                scanned += 1
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
                    st = await self._s(self.storage.get_video_fetch_state, self.account_id, oid)
                # 新视频检测(本地无记录)-> 自动评论+置顶
                is_new = (st is None) or (not st["seen"])
                if is_new and self.auto_commenter:
                    try:
                        await self.auto_commenter.try_comment(oid)
                    except Exception as e:
                        logger.error(f"[{self.account_id}] 自动评论异常 {oid}: {e}")
                if st is not None:
                    # 优化1:接口明确说没评论 -> 跳过(记录快照)。set 内部有单调性保护,
                    # 不会把已有的正数打回 0。
                    if not cc:
                        await self._s(self.storage.set_video_comment_count, self.account_id, oid, 0)
                        continue
                    # 优化2:评论数没增加 -> 跳过。前提是【本地真抓到过评论】:
                    # has_comments=False 说明历史快照可能是脏 0(从未抓过),
                    # 此时必须强制抓一次,否则永远补不回来。
                    prev = st["cc"]
                    if prev is not None and prev == cc and st["has_comments"]:
                        continue
                # 抓评论
                n, new_subs, del_ids = await self._fetch_comments_for_video(oid, on_batch=on_batch)
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
                        await self._s(self.storage.set_video_comment_count, self.account_id, oid, cc)
                if max_videos and scanned >= max_videos:
                    return scanned, total_new, new_comments, deleted_ids
            last_buff = data.get("lastBuff") or ""
            if not last_buff:
                break
        return scanned, total_new, new_comments, deleted_ids

    async def _fetch_comments_for_video(self, export_id, on_batch=None):
        """抓单个视频所有评论(分页 lastBuff)。返回 (新增数, 新评论列表, 已删评论 id 列表)。

        on_batch 给定时,缓冲达阈值即推送并清空;返回的列表仅含未推送完的剩余部分,
        新增数 new_count 是完整总数(流式清空不影响计数)。
        """
        new_count = 0
        new_list = []
        deleted_ids = []
        last_buff = ""
        while True:
            resp = await self.api.fetch_comments(export_id, last_buff=last_buff)
            if not resp or resp.get("__err"):
                logger.warning(f"[{self.account_id}] comment_list 失败 export={export_id}: {resp}")
                break
            data = resp.get("data") or {}
            comments = data.get("comment") or []
            for cmt in comments:
                cid = cmt.get("commentId")
                if not cid:
                    continue
                is_new = not (await self._s(self.storage.is_comment_exists, cid))
                await self._s(self.storage.upsert_comment, self.account_id, export_id, cmt)
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
                        new_list.append(_shape(self.account_id, export_id, cmt))
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
                    await self._s(self.storage.upsert_comment, self.account_id, export_id, sub)
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
                            new_list.append(_shape(self.account_id, export_id, sub))
                            if on_batch and len(new_list) >= self.FLUSH_THRESHOLD:
                                await self._flush(new_list, deleted_ids, on_batch)
            last_buff = data.get("lastBuff") or ""
            if not last_buff or not comments:
                break
        return new_count, new_list, deleted_ids
