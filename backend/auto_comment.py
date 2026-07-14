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
                logger.warning(f"[{self.account['id']}] 自动评论未返回 commentId(标记已发避免重试): {str(resp)[:200]}")
                self.storage.set_auto_commented(self.account["id"], export_id, "")
                return False
            # 置顶
            pin_resp = await self.api.pin_comment(export_id, comment_id)
            if pin_resp and not pin_resp.get("__err"):
                logger.info(f"[{self.account['id']}] 自动评论+置顶 {export_id}: {content}")
            else:
                logger.warning(f"[{self.account['id']}] 置顶失败 {export_id}: {pin_resp}")
            self.storage.set_auto_commented(self.account["id"], export_id, comment_id)
            return True
        except Exception as e:
            logger.error(f"[{self.account['id']}] 自动评论异常 {export_id}: {e}")
            return False
