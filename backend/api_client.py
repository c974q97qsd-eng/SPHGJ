"""视频号助手评论 API 封装。

通过 Playwright page.evaluate(fetch) 调用,自动带登录 cookie(同源)。
API(均在 /micro/interaction/cgi-bin/mmfinderassistant-bin 下):
  post/post_list              视频列表(oid + commentCount + 分页 lastBuff)
  comment/comment_list        评论列表(commentId/昵称/内容/时间/二级评论 + 分页)
  comment/create_comment      回复评论(replyCommentId/content/clientId/rootCommentId)
  comment/update_feed_comment 标记已读(opType/exportId)
"""
import json
import time
import uuid
from urllib.parse import quote

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_URL = "https://channels.weixin.qq.com/micro/interaction/comment"


class WxApiClient:
    def __init__(self, page, account):
        self.page = page
        self.aid = account.get("_aid", "")
        self.finder_id = account.get("_log_finder_id", "")

    def _body(self, **extra):
        b = {
            "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "",
            "_log_finder_id": self.finder_id,
            "rawKeyBuff": "",
            "pluginSessionId": None,
            "scene": 7,
            "reqScene": 7,
        }
        b.update(extra)
        return b

    async def _post(self, path, body):
        url = f"{BASE}/{path}?_aid={self.aid}&_pageUrl={quote(PAGE_URL)}"
        js = """
        async (args) => {
            try {
                const r = await fetch(args.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(args.body),
                    credentials: 'include'
                });
                return await r.text();
            } catch(e) { return JSON.stringify({__err: e.message}); }
        }
        """
        result = await self.page.evaluate(js, {"url": url, "body": body})
        try:
            return json.loads(result)
        except Exception:
            return {"__err": "parse", "raw": (result or "")[:200]}

    async def fetch_video_list(self, last_buff="", only_unread=False, page_size=20):
        """视频列表。返回 {data:{list:[{objectId,commentCount,createTime,...}], ...}}。lastBuff 分页。"""
        return await self._post("post/post_list", self._body(
            pageSize=page_size, onlyUnread=only_unread, needAllCommentCount=True,
            userpageType=13, forMcn=False, lastBuff=last_buff))

    async def fetch_comments(self, export_id, last_buff="", comment_selection=False):
        """评论列表。返回 {data:{comment:[{commentId,commentNickname,commentContent,...}], lastBuff, commentCount}}。"""
        return await self._post("comment/comment_list", self._body(
            lastBuff=last_buff, exportId=export_id,
            commentSelection=comment_selection, forMcn=False))

    async def reply_comment(self, reply_comment_id, content, root_comment_id=None):
        """回复评论。rootCommentId 缺省=replyCommentId(回复一级评论)。"""
        return await self._post("comment/create_comment", self._body(
            replyCommentId=reply_comment_id, content=content,
            clientId=str(uuid.uuid4()),
            rootCommentId=root_comment_id or reply_comment_id))

    async def mark_read(self, export_id, op_type=1):
        return await self._post("comment/update_feed_comment", self._body(
            opType=op_type, exportId=export_id))

    async def post_comment(self, export_id, content):
        """发新评论(作者主动发,不是回复)。replyCommentId/rootCommentId 空,comment 空对象,带 exportId。"""
        return await self._post("comment/create_comment", self._body(
            replyCommentId="", content=content, clientId=str(uuid.uuid4()),
            rootCommentId="", comment={}, exportId=export_id))

    async def pin_comment(self, export_id, comment_id, op_type=1):
        """置顶评论(opType=1 置顶,0 取消)。"""
        return await self._post("comment/set_top_comment", self._body(
            exportId=export_id, commentId=comment_id, opType=op_type))

    async def delete_comment(self, export_id, comment_id):
        """删除评论。"""
        return await self._post("comment/del_comment", self._body(
            exportId=export_id, commentId=comment_id))
