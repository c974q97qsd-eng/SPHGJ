"""关键字自动回复:匹配评论文本 -> 命中调 create_comment 回复。防重复(同评论只回一次)。

config.auto_reply = {
  "enabled": true/false,
  "rules": [{"keyword": "38码", "reply": "38码有货,请私信"}, ...]
}
关键字一行一个(UI 文本框解析),每条配回复内容。
"""
import logging
logger = logging.getLogger("sphgj")


class AutoReply:
    def __init__(self, api_client, storage, account_id, auto_config):
        self.api = api_client
        self.storage = storage
        self.account_id = account_id
        self.auto_config = auto_config or {}

    def is_enabled(self):
        return bool(self.auto_config.get("enabled"))

    def _match(self, content):
        if not content:
            return None
        for rule in self.auto_config.get("rules", []):
            kw = (rule.get("keyword") or "").strip()
            if kw and kw in content:
                return rule
        return None

    async def reply_comment(self, cmt):
        """单条评论:命中关键字则回复。返回是否回复成功。"""
        if not self.is_enabled():
            return False
        cid = cmt.get("commentId")
        content = cmt.get("commentContent", "")
        if not cid or not content:
            return False
        if self.storage.is_replied(cid):
            return False
        rule = self._match(content)
        if not rule:
            return False
        reply_text = (rule.get("reply") or "").strip()
        if not reply_text:
            return False
        try:
            resp = await self.api.reply_comment(cid, reply_text)
            if resp and not resp.get("__err"):
                self.storage.mark_replied(cid)
                logger.info(f"[{self.account_id}] 自动回复 {cid}({content[:15]})-> {reply_text}")
                return True
            logger.warning(f"[{self.account_id}] 回复失败 {cid}: {resp}")
        except Exception as e:
            logger.error(f"[{self.account_id}] 回复异常 {cid}: {e}")
        return False

    async def check_batch(self, comments):
        """批量评论检查回复。返回回复数。"""
        n = 0
        for cmt in comments:
            if await self.reply_comment(cmt):
                n += 1
        return n
