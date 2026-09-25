"""自动发评论:检测新视频 -> 发新评论 + 置顶。每账号自定义评论内容。

config account 字段:
  auto_comment_enabled: bool   是否启用自动评论
  auto_comment_content: str    评论内容(每账号自定义)
"""
import logging
logger = logging.getLogger("sphgj")


class AutoCommenter:
    def __init__(self, api_client, storage, account):
        self.api = api_client
        self.storage = storage
        self.account = account

    def is_enabled(self):
        return bool(self.account.get("auto_comment_enabled")) and bool(self.account.get("auto_comment_content"))

    async def try_comment(self, export_id):
        """新视频:发评论 + 置顶(未发过才发)。返回是否成功。"""
        if not self.is_enabled():
            return False
        if self.storage.is_auto_commented(self.account["id"], export_id):
            return False
        content = self.account["auto_comment_content"]
        try:
            resp = await self.api.post_comment(export_id, content)
            if not resp or resp.get("__err"):
                logger.warning(f"[{self.account['id']}] 自动评论失败 {export_id}: {resp}")
                return False
            # 解析 commentId(响应 data.comment.commentId 或 data.commentId)
            data = resp.get("data") or {}
            comment_id = None
            if isinstance(data, dict):
                cmt = data.get("comment") or {}
                if isinstance(cmt, dict):
                    comment_id = cmt.get("commentId") or cmt.get("comment_id")
                comment_id = comment_id or data.get("commentId") or data.get("comment_id")
            if not comment_id:
                logger.warning(f"[{self.account['id']}] 自动评论未返回 commentId(已标记已发避免重试,并记待确认,抓到该评论后自动补登): {str(resp)[:200]}")
                self.storage.set_auto_commented(self.account["id"], export_id, "")
                # 记待确认:下一轮抓到这条评论时按 export_id+内容补登真实 comment_id
                try:
                    self.storage.mark_own_comment_pending(self.account["id"], export_id, content, "auto_comment")
                except Exception as e:
                    logger.error(f"[{self.account['id']}] 记待确认登记失败 {export_id}: {e}")
                return False
            # 置顶
            pin_resp = await self.api.pin_comment(export_id, comment_id)
            if pin_resp and not pin_resp.get("__err"):
                logger.info(f"[{self.account['id']}] 自动评论+置顶 {export_id}: {content}")
            else:
                logger.warning(f"[{self.account['id']}] 置顶失败 {export_id}: {pin_resp}")
            self.storage.set_auto_commented(self.account["id"], export_id, comment_id)
            # 同时登记到 own_comments:auto_commented 按作品去重,多次评论会覆盖旧 id,
            # 而 own_comments 一条不落(隐藏与溯源都以它为准)
            try:
                self.storage.mark_own_comment(self.account["id"], comment_id, "auto_comment",
                                              content=content, export_id=export_id)
            except Exception as e:
                logger.error(f"[{self.account['id']}] 登记自己评论失败 {comment_id}: {e}")
            return True
        except Exception as e:
            logger.error(f"[{self.account['id']}] 自动评论异常 {export_id}: {e}")
            return False
