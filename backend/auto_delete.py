"""关键字自动删除:匹配评论文本 -> 命中调 del_comment 删除。防重复(同评论只删一次)。

config.auto_delete = {
  "enabled": true/false,
  "keywords": ["关键字1", "关键字2", ...]   # 一行一个(UI 文本框解析)
}
匹配语义:子串包含(关键字 in 评论内容),命中即删。一级/二级评论均处理。
"""
import asyncio
import logging
logger = logging.getLogger("sphgj")


class AutoDelete:
    def __init__(self, api_client, storage, account_id, auto_config):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.update_config(auto_config or {})

    def update_config(self, auto_config):
        self.auto_config = auto_config or {}
        self.keywords = [k.strip() for k in (self.auto_config.get("keywords") or []) if k.strip()]

    def is_enabled(self):
        return bool(self.auto_config.get("enabled"))

    def _match(self, content):
        if not content:
            return None
        for kw in self.keywords:
            if kw and kw in content:
                return kw
        return None

    async def delete_comment(self, cmt, export_id):
        """单条评论:命中关键字则删除。返回是否删除成功。"""
        if not self.is_enabled():
            return False
        cid = cmt.get("commentId")
        content = cmt.get("commentContent", "")
        nickname = cmt.get("commentNickname", "")
        if not cid or not content or not export_id:
            return False
        if await asyncio.to_thread(self.storage.is_deleted, cid):
            return False
        kw = self._match(content)
        if not kw:
            return False
        try:
            resp = await self.api.delete_comment(export_id, cid)
            if resp and not resp.get("__err"):
                await asyncio.to_thread(self.storage.mark_deleted, cid)
                # 写删除记录(命中关键字/内容/昵称/时间),供删除记录卡片展示
                await asyncio.to_thread(self.storage.log_delete,
                                        self.account_id, cid, nickname, content, kw, export_id)
                logger.info(f"[{self.account_id}] 自动删除 {cid}({content[:15]}) 命中关键字 {kw}")
                return True
            logger.warning(f"[{self.account_id}] 删除失败 {cid}: {resp}")
        except Exception as e:
            logger.error(f"[{self.account_id}] 删除异常 {cid}: {e}")
        return False
