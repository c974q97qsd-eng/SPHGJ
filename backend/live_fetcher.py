"""直播大屏抓取:拦截 liveBuild 页面的 get_live_info 响应(直播数据 liveStats)
+ .flv 请求(直播流 URL)。不主动调 API、不抓 DOM。

liveBuild 自动周期调 get_live_info(~5s/次),LiveFetcher 拦截其响应解析 liveStats;
直播流为 FLV(pull-m1.wxlivecdn.com/.../orig.flv),拦截 .flv 请求取 URL,前端 flv.js 播放。
"""
import asyncio
import json
import logging
from datetime import datetime
from .selectors import LIVE_URL

logger = logging.getLogger("sphgj")


class LiveFetcher:
    def __init__(self, page, account_id):
        self.page = page
        self.account_id = account_id
        self.live_stats = None
        self.stream_url = None
        self.updated_at = None
        page.on("request", self._on_req)
        page.on("response", self._on_resp)

    def _on_req(self, req):
        u = req.url or ""
        # 直播 FLV 流(pull-m1.wxlivecdn.com / trtc)
        if ".flv" in u and ("wxlivecdn" in u or "trtc" in u):
            self.stream_url = u

    def _on_resp(self, resp):
        u = resp.url or ""
        if "get_live_info" in u and "channels.weixin.qq.com" in u:
            try:
                asyncio.create_task(self._capture(resp))
            except Exception as e:
                logger.debug(f"[live:{self.account_id}] 调度响应抓取失败: {e}")

    async def _capture(self, resp):
        try:
            txt = await resp.text()
            data = json.loads(txt)
            stats = (data.get("data") or {}).get("liveStats")
            if stats:
                self.live_stats = stats
                self.updated_at = datetime.now().isoformat()
        except Exception as e:
            logger.debug(f"[live:{self.account_id}] get_live_info 解析失败: {e}")

    async def goto_live(self):
        try:
            await self.page.goto(LIVE_URL, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"[live:{self.account_id}] goto liveBuild 失败: {e}")

    async def fetch(self):
        return {
            "live_stats": self.live_stats,
            "stream_url": self.stream_url,
            "updated_at": self.updated_at,
        }
