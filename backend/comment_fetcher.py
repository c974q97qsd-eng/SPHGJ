"""评论抓取引擎:遍历视频 -> 增量抓评论 -> 存 SQLite。

优化(减少 comment_list 调用):
  1. post_list 返回 commentCount,=0 的视频直接跳过
  2. 本地存每视频上次 commentCount,没增加的跳过
  3. 评论按 commentId 去重(已存在的 upsert,不重复计数)

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

    async def fetch_all(self, max_videos=None):
        """遍历所有视频,增量抓评论。返回 (扫描视频数, 新增评论数, 新评论列表, 已删评论 id 列表)。"""
        scanned = 0
        new_comments = []
        deleted_ids = []
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
                cc = v.get("commentCount", 0) or 0
                if not oid:
                    continue
                scanned += 1
                # 新视频检测(本地无记录)-> 自动评论+置顶
                is_new = (await self._s(self.storage.get_video_comment_count, self.account_id, oid)) is None
                if is_new and self.auto_commenter:
                    try:
                        await self.auto_commenter.try_comment(oid)
                    except Exception as e:
                        logger.error(f"[{self.account_id}] 自动评论异常 {oid}: {e}")
                # 优化1:没评论的跳过(仍记录评论数=0)
                if not cc:
                    await self._s(self.storage.set_video_comment_count, self.account_id, oid, 0)
                    continue
                # 优化2:评论数没增加的跳过
                prev = await self._s(self.storage.get_video_comment_count, self.account_id, oid)
                if prev is not None and prev == cc:
                    continue
                # 抓评论
                n, new_subs, del_ids = await self._fetch_comments_for_video(oid)
                new_comments.extend(new_subs)
                deleted_ids.extend(del_ids)
                await self._s(self.storage.set_video_comment_count, self.account_id, oid, cc)
                if max_videos and scanned >= max_videos:
                    return scanned, len(new_comments), new_comments, deleted_ids
            last_buff = data.get("lastBuff") or ""
            if not last_buff:
                break
        return scanned, len(new_comments), new_comments, deleted_ids

    async def _fetch_comments_for_video(self, export_id):
        """抓单个视频所有评论(分页 lastBuff)。返回 (新增数, 新评论列表, 已删评论 id 列表)。"""
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
                        # 未删的新评论触发自动回复
                        if self.auto_reply:
                            try:
                                await self.auto_reply.reply_comment(cmt)
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
            last_buff = data.get("lastBuff") or ""
            if not last_buff or not comments:
                break
        return new_count, new_list, deleted_ids
