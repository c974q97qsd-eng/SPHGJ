"""视频号助手评论 API 封装。

rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py),写接口默认仍走浏览器。
rev51c: 写接口支持按账号灰度(
        risk_control.api_write_http_accounts=["uspoloassn1"]),
        且写操作的 HTTP 失败**不自动重试**(避免重复评论),详见 _http_give_up。
      两条路返回结构完全一致,调用方无需感知。HTTP 任何异常自动回退浏览器。
      2026-10-28 实测:纯 HTTP 与页面内 fetch 同源同 cookie,只是少了跨进程 IPC。
      原说明如下 —— 通过 Playwright page.evaluate(fetch) 调用,自动带登录 cookie(同源)。
API(均在 /micro/interaction/cgi-bin/mmfinderassistant-bin 下):
  post/post_list              视频列表(oid + commentCount + 分页 lastBuff)
  comment/comment_list        评论列表(commentId/昵称/内容/时间/二级评论 + 分页)
  comment/create_comment      回复评论(replyCommentId/content/clientId/rootCommentId)
  comment/update_feed_comment 标记已读(opType/exportId)
"""
import json
import re
import time
import random
import asyncio
import logging
import uuid
from urllib.parse import quote

from . import wx_http

logger = logging.getLogger("sphgj")

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
AUTH_BASE = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin"  # 认证/账号类接口(auth_data 等,路径无 micro/interaction)
PAGE_URL = "https://channels.weixin.qq.com/micro/interaction/comment"
# 内容管理页(作品列表/隐藏/置顶走这里,官方前端同源)
CONTENT_BASE = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin"
POST_PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"


class WxApiClient:
    # 防风控:读/写请求间隔(秒,随机区间)。写操作(create/del/set_top)更保守。
    # 视频号风控阈值不公开,这里按腾讯系通用规律取偏保守值,可在 config.risk_control 覆盖。
    READ_INTERVAL = (1.0, 2.5)
    WRITE_INTERVAL = (4.0, 8.0)
    WRITE_PATHS = ("create_comment", "set_top_comment", "del_comment", "update_visible", "update_sticky_status")
    BACKOFF_STEPS = (30, 120, 600)  # 连续失败退避秒数:30s -> 2min -> 10min(封顶)

    def __init__(self, page, account, config=None, storage=None):
        self.page = page
        # rev53: 保留创建时的账号引用。_latest_account() 的兜底分支 return self.account,
        # 但这个方法从 rev50 起就没人赋值过 —— config["accounts"] 里查不到该 id 时
        # 直接抛 AttributeError(_post_via_http 里没有 try 兜),而 _post 的注释写的是
        # 「任何 HTTP 侧异常一律回退浏览器」,实际会炸穿整轮抓取。
        # 触发条件:account_id 为空(新卡片未绑定) / 账号被改名或删除 / 测试桩只传
        # 局部 config。线上因为 config 里都能查到所以一直没暴露。
        self.account = account or {}
        self.aid = account.get("_aid", "")
        self.finder_id = account.get("_log_finder_id", "")
        self.account_id = account.get("id", "")
        self._storage = storage
        self._last_req_time = 0.0
        self.config = config or {}
        rc = ((config or {}).get("risk_control")) or {}
        # rev50: 读/写分流。读走 HTTP 直连,写默认仍走浏览器 —— 写操作的风控表现
        # 没有任何实测证据(TLS 指纹与真浏览器不同),先留退路,要切再改配置。
        self._read_via = str(rc.get("api_read_via") or "http").lower()
        self._write_via = str(rc.get("api_write_via") or "browser").lower()
        # rev51c: 写操作灰度名单。填 account id 或视频号短名都行,默认空 = 全走浏览器。
        #   risk_control.api_write_http_accounts = ["uspoloassn1"]
        _g = rc.get("api_write_http_accounts") or []
        if isinstance(_g, str):
            _g = [x.strip() for x in _g.split(",") if x.strip()]
        self._write_http_accounts = {str(x) for x in _g}
        self._http_block_until = 0.0     # no_cookie 时短期禁用,别每轮白跑
        self._http_last_why = ""
        self._http_logged = False        # 首次 HTTP 成功打一条 INFO,线上据此确认在跑
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
        # rev52: 视频号接口的业务错误码在 errCode 上,原来这里只看 ret ——
        # errCode=-1 时被当成"成功",既不退避也不计数,于是连续被拒还照常猛打。
        # 线上实测 2 号重扫后 20 分钟内出现 4 次 errCode=-1(post_list/comment_list 各 2 次)。
        code = self._err_code(resp)
        if code not in (0, None):
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

    async def _post(self, path, body, base=None, page_url=None):
        # 写操作每日上限:超限跳过,不调 API(不计退避)
        is_write = self._is_write(path)
        today = time.strftime("%Y-%m-%d") if is_write else None
        if is_write and self._daily_count(today) >= self._daily_write_limit:
            logger.warning(f"[api] 写操作达每日上限 {self._daily_write_limit},跳过 {path}")
            return {"__err": "daily_write_limit", "limit": self._daily_write_limit}
        await self._throttle(path)
        base = base or BASE
        page_url = page_url or PAGE_URL
        url = f"{base}/{path}?_aid={self.aid}&_pageUrl={quote(page_url)}"
        # rev50: 读走 HTTP 直连(快、不占 renderer),写默认仍走浏览器。
        # 任何 HTTP 侧异常一律回退浏览器 —— 页面里是"活会话",永远比 storage_state 新。
        resp = None
        if self._via_http(is_write):
            resp = await self._post_via_http(path, url, body, page_url, is_write=is_write)
        if resp is None:
            resp = await self._post_via_page(path, url, body)
        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
            self._on_failure()
        else:
            self._on_success()
            if is_write:
                self._incr_daily_count(today)
        return resp

    # ==================== rev50: HTTP 直连 ====================

    def _account_keys(self):
        """可用于灰度匹配的账号标识集合(id / 视频号短名 / aid)。"""
        acc = self._latest_account() or {}
        keys = {
            str(self.account_id or ""),
            str(acc.get("id") or ""),
            str(acc.get("_log_finder_id") or ""),
            str(self.finder_id or ""),
            str(acc.get("_aid") or ""),
        }
        return {k for k in keys if k}

    def _via_http(self, is_write):
        """本次走 HTTP 还是浏览器。

        读: 默认 http(_read_via)。
        写: 默认 browser;全局 api_write_via="http" 或账号命中灰度名单才走 http。
        """
        if time.time() < self._http_block_until:
            return False
        if not is_write:
            return self._read_via == "http"
        if self._write_via == "http":
            return True
        if not self._write_http_accounts:
            return False
        return bool(self._account_keys() & self._write_http_accounts)

    def _latest_account(self):
        """每次现取 config 里的账号 —— 重新扫码后 storage_state / _aid 会被换成新的,
        握着的旧引用会拿到过期 cookie(rev37 踩过:worker 以为登录着,HTTP 侧是空的)。"""
        aid = self.account_id
        if aid:
            for a in (self.config.get("accounts") or []):
                if a.get("id") == aid:
                    return a
        return self.account

    @staticmethod
    def _err_code(resp):
        """取 errCode(ret 也认)。取不到返回 None。"""
        if not isinstance(resp, dict):
            return None
        for key in ("errCode", "ret"):
            v = resp.get(key)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        base = resp.get("base_resp")
        if isinstance(base, dict):
            v = base.get("ret")
            if isinstance(v, int):
                return v
        return None

    def _http_give_up(self, why, path, long_block=False, is_write=False):
        """HTTP 不可用 -> 记录原因。

        读操作: 返回 None,调用方回退浏览器重取(幂等,安全)。
        写操作: 只有 no_cookie(请求根本没发出去)可以安全回退;
                其余情况一律**不回退**,返回带 __err 的 dict ——
                HTTP 超时时服务端可能已经处理了,重发会造成重复评论/重复删除。
        """
        if long_block:
            # cookie 不会在 3 分钟内自己长回来,别每轮白跑一次 HTTP
            self._http_block_until = time.time() + 180
        if is_write and why != "no_cookie":
            logger.warning(f"[api] 写操作 HTTP 失败({why}) path={path} -> "
                           f"不重试(避免重复提交),按失败处理")
            return {"__err": f"http_{why}", "__no_retry": True, "path": path}
        if self._http_last_why != why:
            self._http_last_why = why
            logger.info(f"[api] HTTP 直连不可用({why}),回退浏览器 path={path}"
                        + ("(180s 内不再尝试)" if long_block else ""))
        return None

    async def _post_via_http(self, path, url, body, page_url, is_write=False):
        """进程内 HTTP 直连。返回 dict;None = 走不通,请回退浏览器。

        读操作: 只认「成功」,任何异常都退浏览器重试(幂等)。
        写操作: 见 _http_give_up —— 除 no_cookie 外一律不重试,避免重复提交。
        """
        try:
            acc = self._latest_account()
            # _aid 每次登录都会变;URL 是早先拼的,这里按最新值纠正
            aid = (acc.get("_aid") or "").strip()
            if aid and aid != self.aid:
                url = re.sub(r"_aid=[^&]+", "_aid=" + aid, url, count=1)
            resp = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=30)
        except Exception as e:
            return self._http_give_up("exception:" + repr(e)[:60], path,
                                      is_write=is_write)
        err = resp.get("__err") if isinstance(resp, dict) else None
        if err == "no_cookie":
            # 本地就没有登录态,请求必然没发出去 -> 写操作也能安全回退
            return self._http_give_up("no_cookie", path, long_block=True)
        if err:
            return self._http_give_up(err, path, is_write=is_write)
        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333),
                                      is_write=is_write)
        if not self._http_logged:
            self._http_logged = True
            kind = "写" if is_write else "读"
            logger.info(f"[api] HTTP 直连可用(首次{kind}操作成功 path={path}, "
                        f"backend={wx_http.backend_name()}),{kind}接口走进程内请求")
        return resp

    def http_available(self):
        """本地有没有可用于 HTTP 直连的登录态。不访问网络,纯查 config。

        rev52: 抓取流程要靠它决定「这轮要不要开浏览器」。有 storage_state 就先走纯
        HTTP(零 Chromium);没有(或已过期)才走 ensure_browser 那条老路。

        也要尊重 `_http_block_until` —— no_cookie / errCode=300333 之后会阻塞 180s,
        这段时间 cookie 不会自己长回来,再试只是白跑一次请求。
        """
        if time.time() < self._http_block_until:
            return False
        try:
            return bool(wx_http.cookie_header_of(self._latest_account()))
        except Exception:
            return False

    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
        if self.page is None:
            # rev52: 纯 HTTP 模式(未开浏览器)下的回退请求。不能抛异常 ——
            # 抛出去会让整轮抓取炸掉,而正确语义只是「这条走不通」。
            return {"__err": "no_page"}
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
            result = await asyncio.wait_for(
                self.page.evaluate(js, {"url": url, "body": body}), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(f"[api] evaluate 超时(30s) path={path}")
            return {"__err": "eval_timeout"}
        except Exception as e:
            logger.warning(f"[api] evaluate 异常 path={path}: {e}")
            return {"__err": "eval_error"}
        try:
            return json.loads(result)
        except Exception:
            return {"__err": "parse", "raw": (result or "")[:200]}

    def write_blocked(self):
        """今日写操作是否已达上限。

        rev52: 供上层在每轮/每视频判断前先问一句,避免明知会被拒还逐条调用
        (原来 auto_comment 对每个新视频都 try_comment -> post_comment 被上限挡回,
        线上一天打了 5574 条 WARNING,把日志淹掉)。
        """
        today = time.strftime("%Y-%m-%d")
        try:
            return self._daily_count(today) >= self._daily_write_limit
        except Exception:
            return False

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
        """调 auth/auth_data(POST 空 body)取登录微信昵称(data.userAttr.nickname)。

        rev50: 先试 HTTP 直连(这条读路径同样不需要浏览器),失败回退页面 fetch。
        """
        await self._throttle("auth/auth_data")
        acc = self._latest_account()
        aid = (acc.get("_aid") or self.aid or "").strip()
        url = f"{AUTH_BASE}/auth/auth_data?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
        try:
            j = await wx_http.post_json(acc, url, {}, page_url=PAGE_URL, timeout=20)
            if not j.get("__err") and self._err_code(j) in (0, None):
                nick = ((j.get("data") or {}).get("userAttr") or {}).get("nickname") or ""
                if nick:
                    return nick
        except Exception:
            pass
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

    async def fetch_video_list(self, last_buff="", only_unread=False, page_size=None):
        """视频列表。返回 {data:{list:[{objectId,commentCount,createTime,...}], ...}}。lastBuff 分页。

        rev52: pageSize 默认 20 -> 100。这是本项目请求量最大的一条路径 ——
        每轮都要翻完全部作品,pageSize=20 时 8 个账号合计 348 页/轮(白天 10 分钟一轮
        = 41760 次/天,占总请求 88%,单轮串行耗时 10.1 分钟已把 10 分钟的周期额度吃满)。
        实测 100 条/页可用:返回 100 条、errCode=0、约 1 秒、427KB,连翻 3 页无重复
        且时间连续(08-31 11:01 跨页正确衔接)。响应体大小与 20 条/页按比例放大,
        总流量不变,省的是请求数(348->70 页/轮)与节流等待。
        上限按 100 封顶:再大未实测,不拿线上账号试。
        """
        if page_size is None:
            try:
                page_size = int((self.config or {}).get("post_list_page_size", 100) or 100)
            except (TypeError, ValueError):
                page_size = 100
        page_size = max(1, min(100, page_size))
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

    # ==================== 作品管理(内容管理页接口,2026-09-15 探测实锤) ====================
    async def fetch_post_list(self, page=1, page_size=200):
        """作品列表(内容管理页口径)。返回 data.list,含 readCount/visibleType/stickyOpStatus。"""
        return await self._post("post/post_list", self._body(
            pageSize=page_size, currentPage=page, userpageType=11, stickyOrder=True),
            base=self.CONTENT_BASE, page_url=self.POST_PAGE_URL)

    async def update_post_visible(self, object_id, visible_type):
        """改作品可见性。visible_type: 1=公开 3=仅自己可见(2=仅粉丝)。"""
        return await self._post("post/post_update_visible", self._body(
            objectId=object_id, visibleType=visible_type),
            base=self.CONTENT_BASE, page_url=self.POST_PAGE_URL)

    async def update_post_sticky(self, export_id, sticky_op):
        """置顶/取消置顶。sticky_op: 1=Sticky(置顶) 2=UnSticky(取消)。0=NoOperation 不可操作。"""
        return await self._post("post/update_sticky_status", self._body(
            exportId=export_id, stickyOp=sticky_op),
            base=self.CONTENT_BASE, page_url=self.POST_PAGE_URL)
