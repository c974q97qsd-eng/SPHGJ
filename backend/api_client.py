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
import random
import asyncio
import logging
import uuid
from urllib.parse import quote

logger = logging.getLogger("sphgj")

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
AUTH_BASE = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin"  # 认证/账号类接口(auth_data 等,路径无 micro/interaction)
PAGE_URL = "https://channels.weixin.qq.com/micro/interaction/comment"


class WxApiClient:
    # 防风控:读/写请求间隔(秒,随机区间)。写操作(create/del/set_top)更保守。
    # 视频号风控阈值不公开,这里按腾讯系通用规律取偏保守值,可在 config.risk_control 覆盖。
    READ_INTERVAL = (1.0, 2.5)
    WRITE_INTERVAL = (4.0, 8.0)
    WRITE_PATHS = ("create_comment", "set_top_comment", "del_comment")
    BACKOFF_STEPS = (30, 120, 600)  # 连续失败退避秒数:30s -> 2min -> 10min(封顶)

    def __init__(self, page, account, config=None, storage=None):
        self.page = page
        self.aid = account.get("_aid", "")
        self.finder_id = account.get("_log_finder_id", "")
        self.account_id = account.get("id", "")
        self._storage = storage
        self._last_req_time = 0.0
        rc = ((config or {}).get("risk_control")) or {}
        ri = rc.get("read_interval") or list(self.READ_INTERVAL)
        wi = rc.get("write_interval") or list(self.WRITE_INTERVAL)
        self._read_interval = (float(ri[0]), float(ri[1])) if isinstance(ri, (list, tuple)) and len(ri) >= 2 else self.READ_INTERVAL
        self._write_interval = (float(wi[0]), float(wi[1])) if isinstance(wi, (list, tuple)) and len(wi) >= 2 else self.WRITE_INTERVAL
        # 失败退避 + 写操作每日上限
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self._daily_writes = 0
        self._daily_writes_day = None
        self._daily_write_limit = int(rc.get("daily_write_limit", 100))

    async def _throttle(self, path):
        """防风控节流:退避期先等到结束,再按读/写随机间隔。"""
        now = time.time()
        if self._backoff_until and now < self._backoff_until:
            await asyncio.sleep(self._backoff_until - now)
        is_write = any(w in path for w in self.WRITE_PATHS)
        lo, hi = self._write_interval if is_write else self._read_interval
        wait = random.uniform(lo, hi)
        elapsed = time.time() - self._last_req_time
        if elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_req_time = time.time()

    def _is_write(self, path):
        return any(w in path for w in self.WRITE_PATHS)

    def _is_failure(self, resp):
        """判定请求失败(网络/解析错误或 ret 非 0)。daily_write_limit 不算失败(自限非风控)。"""
        if not isinstance(resp, dict):
            return True
        err = resp.get("__err")
        if err and err != "daily_write_limit":
            return True
        if resp.get("ret") not in (None, 0):
            return True
        base = resp.get("base_resp")
        if isinstance(base, dict) and base.get("ret") not in (None, 0):
            return True
        return False

    def _on_failure(self):
        self._consecutive_failures += 1
        idx = min(self._consecutive_failures - 1, len(self.BACKOFF_STEPS) - 1)
        secs = self.BACKOFF_STEPS[idx]
        self._backoff_until = time.time() + secs
        logger.warning(f"[api] 请求失败(连续 {self._consecutive_failures} 次),退避 {secs}s")

    def _on_success(self):
        if self._consecutive_failures:
            self._consecutive_failures = 0
            self._backoff_until = 0.0

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
        # 写操作每日上限:超限跳过,不调 API(不计退避)
        is_write = self._is_write(path)
        today = time.strftime("%Y-%m-%d") if is_write else None
        if is_write and self._daily_count(today) >= self._daily_write_limit:
            logger.warning(f"[api] 写操作达每日上限 {self._daily_write_limit},跳过 {path}")
            return {"__err": "daily_write_limit", "limit": self._daily_write_limit}
        await self._throttle(path)
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
        try:
            result = await asyncio.wait_for(self.page.evaluate(js, {"url": url, "body": body}), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[api] evaluate 超时(30s) path={path}")
            return {"__err": "eval_timeout"}
        try:
            resp = json.loads(result)
        except Exception:
            resp = {"__err": "parse", "raw": (result or "")[:200]}
        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
            self._on_failure()
        else:
            self._on_success()
            if is_write:
                self._incr_daily_count(today)
        return resp

    def _daily_count(self, today):
        """今日写计数:有 storage 走持久化,否则内存兜底(按日期重置)。"""
        if self._storage:
            return self._storage.get_daily_write_count(self.account_id, today)
        if today != self._daily_writes_day:
            self._daily_writes_day = today
            self._daily_writes = 0
        return self._daily_writes

    def _incr_daily_count(self, today):
        if self._storage:
            self._storage.incr_daily_write_count(self.account_id, today)
        else:
            self._daily_writes += 1

    async def fetch_wx_name(self):
        """调 auth/auth_data(POST 空 body)取登录微信昵称(data.userAttr.nickname)。"""
        await self._throttle("auth/auth_data")
        url = f"{AUTH_BASE}/auth/auth_data?_aid={self.aid}&_pageUrl={quote(PAGE_URL)}"
        js = """
        async (args) => {
            try {
                const r = await fetch(args.url, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: '{}', credentials: 'include'
                });
                return await r.text();
            } catch(e) { return JSON.stringify({__err: e.message}); }
        }
        """
        result = await self.page.evaluate(js, {"url": url})
        try:
            data = json.loads(result)
            return ((data.get("data") or {}).get("userAttr") or {}).get("nickname") or ""
        except Exception:
            return ""

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
