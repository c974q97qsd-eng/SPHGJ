"""抖音创作者中心客户端 —— 只负责「把请求送出去」，接口规格全在 api.py。

为什么只做 page-mode
-------------------
真机实测：抖音写操作要带 `x-secsdk-csrf-token`（由 `mssdk.bytedance.com/web/common`
换取、与页面会话绑定），而**页内同源 fetch 天然带齐**这套凭证（回复 / 删除 / 置顶 /
发布四条写链路都实测成功）。所以 rev56 只实现一种传输：在已登录的浏览器页面里
`fetch()`。纯 httpx 需要额外复刻 csrf 与 msToken 链路，收益只是省一次 IPC，
在「写操作必须正确」面前不值当；读接口将来要加速，加一个 HttpTransport 即可，
_request() 只依赖 transport，不关心底下是什么。

页面协议（duck typing，不 import playwright）
--------------------------------------------
    page.evaluate(expression: str, arg) -> awaitable
Playwright 的 page 就是这个签名，所以生产环境直接传 `worker.page`；
本地没装 playwright 时，用 tools/dyprobe/cdp_page.py 里的 CDP 适配器顶上。

失败语义
--------
每个方法的返回都是统一的信封：

    {"__ok": True,  "__err": "",   ...业务字段...}
    {"__ok": False, "__err": "<api.ERR_*>", "__msg": "<人话>", "raw": <原始>}

调用方**只看 __ok**，不要自己解析 status_code（坑 1）。
"""
import asyncio
import json
import logging
import random
import time

from . import api

logger = logging.getLogger("sphgj")


class PageTransport:
    """在页面里执行同源 fetch。唯一依赖：page.evaluate(expr, arg)。"""

    #: 通用 fetch 包装。参数全部显式传进去，避免字符串拼接注入。
    JS = r"""
    async (a) => {
        try {
            const opt = { method: a.method, credentials: 'include' };
            if (a.body !== null && a.body !== undefined) {
                if (a.form) {
                    opt.headers = { 'content-type': 'application/x-www-form-urlencoded' };
                    opt.body = a.body;
                } else {
                    opt.headers = { 'content-type': 'application/json' };
                    opt.body = a.body;
                }
            }
            const r = await fetch(a.url, opt);
            const t = await r.text();
            return JSON.stringify({ status: r.status, text: t });
        } catch (e) {
            return JSON.stringify({ status: -1, text: 'ERR ' + String(e) });
        }
    }
    """

    def __init__(self, page, timeout=45):
        self.page = page
        self.timeout = timeout

    async def send(self, method, url, body=None, form=False):
        """返回 (http_status, text)。http_status=-1 表示页面执行层面就失败了。"""
        if self.page is None:
            return -1, "ERR no_page"
        payload = {
            "method": method,
            "url": url,
            "form": bool(form),
            "body": None if body is None else (api.dumps_form(body) if form else api.dumps_json(body)),
        }
        try:
            raw = await asyncio.wait_for(self.page.evaluate(self.JS, payload),
                                         timeout=self.timeout)
        except asyncio.TimeoutError:
            return -1, "ERR eval_timeout(%ss)" % self.timeout
        except Exception as e:
            return -1, "ERR eval_exception " + repr(e)[:120]
        try:
            d = json.loads(raw)
        except Exception:
            return -1, "ERR bad_wrapper " + str(raw)[:120]
        return int(d.get("status", -1)), str(d.get("text") or "")


class DouyinClient:
    #: 防风控随机间隔（秒）。抖音风控阈值不公开，取比视频号更保守的值；
    #: 可在 config.risk_control.dy_read_interval / dy_write_interval 覆盖。
    READ_INTERVAL = (1.0, 2.2)
    WRITE_INTERVAL = (3.5, 7.0)
    BACKOFF_STEPS = (30, 120, 600)

    def __init__(self, page, account, config=None, storage=None, transport=None):
        self.page = page
        self.account = account or {}
        self.account_id = str(self.account.get("id") or "")
        self.uid = str(self.account.get("_dy_uid") or "")
        self.sec_uid = str(self.account.get("_dy_sec_uid") or "")
        self.config = config or {}
        self._storage = storage
        self.transport = transport or PageTransport(page)
        rc = (self.config.get("risk_control") or {})
        self._read_interval = self._pair(rc.get("dy_read_interval"), self.READ_INTERVAL)
        self._write_interval = self._pair(rc.get("dy_write_interval"), self.WRITE_INTERVAL)
        self._daily_write_limit = self._int(rc.get("daily_write_limit"), 100)
        self._last_req = 0.0
        self._consec_fail = 0
        self._backoff_until = 0.0
        self._daily_writes = 0
        self._daily_day = None
        #: 登录态已被服务端明确判死（status_code=8）。上层据此判掉线，别再发包。
        self.auth_fail = None
        #: 明文 aweme_id -> 加密 item_id 缓存。加密结果对同一个 aweme_id 是稳定的，
        #: 免得每轮都从第 1 页翻起（91 条作品 = 5 页，纯浪费）。上限 2000 条足够。
        self._item_enc = {}

    # ---------------- 小工具 ----------------
    @staticmethod
    def _pair(v, default):
        try:
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                return (float(v[0]), float(v[1]))
        except (TypeError, ValueError):
            pass
        return default

    @staticmethod
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def set_page(self, page):
        """绑定/解绑页面（关 context 后必须调 set_page(None)，否则拿着死 page 白发请求）。"""
        self.page = page
        self.transport = PageTransport(page)

    # ---------------- 节流 / 退避 / 写上限 ----------------
    async def _throttle(self, is_write):
        now = time.time()
        if self._backoff_until and now < self._backoff_until:
            await asyncio.sleep(self._backoff_until - now)
        lo, hi = self._write_interval if is_write else self._read_interval
        wait = random.uniform(lo, hi)
        elapsed = time.time() - self._last_req
        if elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_req = time.time()

    def _count_today(self):
        day = time.strftime("%Y-%m-%d")
        if self._storage is not None:
            try:
                return self._storage.get_daily_write_count(self.account_id, day)
            except Exception:
                pass
        if day != self._daily_day:
            self._daily_day, self._daily_writes = day, 0
        return self._daily_writes

    def _incr_today(self):
        day = time.strftime("%Y-%m-%d")
        if self._storage is not None:
            try:
                self._storage.incr_daily_write_count(self.account_id, day)
                return
            except Exception:
                pass
        if day != self._daily_day:
            self._daily_day, self._daily_writes = day, 0
        self._daily_writes += 1

    def write_blocked(self):
        """今日写操作是否达上限。上层在逐条写之前先问一句，别明知会被拦还硬打。"""
        try:
            return self._count_today() >= self._daily_write_limit
        except Exception:
            return False

    def _fail(self, reason, msg="", raw=None, is_write=False):
        if reason == api.ERR_NOT_LOGGED_IN:
            self.auth_fail = {"reason": reason, "ts": time.time()}
        if not is_write and reason == api.ERR_NOT_LOGGED_IN:
            pass  # 读失败交给上层判掉线；这里只标记
        self._consec_fail += 1
        idx = min(self._consec_fail - 1, len(self.BACKOFF_STEPS) - 1)
        secs = self.BACKOFF_STEPS[idx]
        self._backoff_until = time.time() + secs
        logger.warning("[dy] 请求失败(%s)·连续%d次·退避%ds%s"
                       % (reason, self._consec_fail, secs,
                          ("·write" if is_write else "")))
        return {"__ok": False, "__err": reason,
                "__msg": msg or api.explain(reason), "raw": raw}

    def _ok(self, raw):
        """成功也统一成信封 —— 调用方只认 __ok，永远不用猜这一层返回的是什么形状。"""
        if self._consec_fail:
            self._consec_fail = 0
            self._backoff_until = 0.0
        self.auth_fail = None
        return {"__ok": True, "__err": "", "raw": raw}

    # ---------------- 统一请求 ----------------
    async def _request(self, method, url, body=None, form=False, is_write=False):
        if is_write and self.write_blocked():
            logger.warning("[dy] 写操作达每日上限 %d，跳过 %s"
                           % (self._daily_write_limit, url.split("?")[0]))
            return {"__ok": False, "__err": "daily_write_limit",
                    "__msg": "今日写操作已达上限"}
        await self._throttle(is_write)
        status, text = await self.transport.send(method, url, body=body, form=form)
        if status == -1:
            return self._fail(api.ERR_EVAL, text[:160], raw=text, is_write=is_write)
        if status >= 400:
            return self._fail(api.ERR_HTTP, "HTTP %d" % status, raw=text[:400],
                              is_write=is_write)
        try:
            resp = json.loads(text)
        except Exception:
            return self._fail(api.ERR_PARSE, "非 JSON 响应", raw=text[:300],
                              is_write=is_write)
        ok, reason = api.judge(resp)
        if not ok:
            env = self._fail(reason, api.explain(reason), raw=resp, is_write=is_write)
            env["status_code"] = api.status_code_of(resp)
            return env
        if is_write:
            self._incr_today()
        return self._ok(resp)

    # ==================== 作品 ====================
    async def fetch_work_page(self, max_cursor=0, count=12, status=0):
        """一页作品列表。

        返回 {"__ok", "works":[...], "max_cursor":<毫秒时间戳>, "has_more", "total"}。
        """
        env = await self._request("GET", api.work_list_url(max_cursor=max_cursor,
                                                          count=count, status=status))
        if not env["__ok"]:
            return env
        works, nxt, more, total = api.works_of(env["raw"])
        return {"__ok": True, "works": works, "max_cursor": nxt,
                "has_more": more, "total": total, "raw": env["raw"]}

    async def fetch_all_works(self, max_pages=40, count=12, status=0):
        """翻完所有作品（按 max_cursor 时间戳游标）。返回 (works, complete)。

        complete=False 表示中途有页失败 —— 上层必须当「不完整」处理，
        不能把残缺列表当成全量（会误判作品被删）。
        """
        out, cursor, pages = [], 0, 0
        while pages < max_pages:
            env = await self.fetch_work_page(max_cursor=cursor, count=count, status=status)
            pages += 1
            if not env["__ok"]:
                logger.warning("[dy] work_list 第%d页失败(%s)，本轮作品列表不完整"
                               % (pages, env["__err"]))
                return out, False
            batch = env["works"]
            out.extend(batch)
            if not env["has_more"] or not batch:
                return out, True
            nxt = env["max_cursor"]
            # 游标没前进 = 服务端到底了或参数被忽略，必须停，否则死循环
            if not nxt or nxt == cursor:
                return out, True
            cursor = nxt
        return out, True

    async def fetch_item_page(self, cursor="", count=20):
        """一页 item/list。返回 {"__ok","map","cursor","has_more","raw"}。

        🚨 游标键是 `cursor`（时间戳字符串）；`max_cursor` 只是兼容别名，
        item/list 真机上从来没有这个字段（坑 4）。新代码一律用 cursor。
        """
        env = await self._request("GET", api.item_list_url(cursor=cursor, count=count))
        if not env["__ok"]:
            return env
        m, nxt, more = api.item_map_of(env["raw"])
        return {"__ok": True, "map": m, "cursor": nxt, "max_cursor": nxt,
                "has_more": more, "raw": env["raw"]}

    async def map_item_ids(self, aweme_ids, max_pages=30, count=20, use_cache=True):
        """建 明文 aweme_id -> 加密 item_id 映射（评论接口只认加密串）。

        只翻到「需要的 id 全部找到」或没更多为止；找不到的留给调用方跳过并记日志，
        绝不拿明文 id 硬试（明文一定失败，白打一次请求）。

        🚨 这里的游标是 item/list 的 `cursor`（时间戳字符串），不是 work_list 的
        max_cursor（坑 4）。读错字段会永远读到 0 -> 第一页就 break -> 只覆盖 19 条。
        顺序是发布时间倒序，所以「只要最近的作品」通常 1 页就够；老作品才需要翻深。

        use_cache: 命中进程内缓存就不发请求。加密串对同一 aweme_id 稳定，
        缓存不会过期；但**换账号必须清缓存**（spec 里 item_id 与登录会话相关），
        见 clear_item_cache()。
        """
        want = {str(x) for x in (aweme_ids or []) if x}
        found = {}
        if use_cache:
            found = {k: self._item_enc[k] for k in want if k in self._item_enc}
        todo = want - set(found)
        if not todo:
            return found

        cursor, pages = "", 0
        while pages < max_pages:
            env = await self.fetch_item_page(cursor=cursor, count=count)
            pages += 1
            if not env["__ok"]:
                break
            for plain, enc in env["map"].items():
                if plain in todo:
                    found[plain] = enc
                    if len(self._item_enc) < 2000:
                        self._item_enc[plain] = enc
            if len(found) >= len(want):
                break
            if not env["has_more"]:
                break
            nxt = env.get("cursor") or env.get("max_cursor") or ""
            if not nxt or str(nxt) == str(cursor):
                break
            cursor = nxt
        return found

    def clear_item_cache(self):
        """换账号 / 登录态变化后必须清，否则拿着别人的加密串去打评论接口。"""
        self._item_enc = {}

    async def self_identity(self):
        """取当前登录账号的身份（uid / sec_uid / unique_id / nickname）。

        来源是**自己的作品列表**里的 author 字段（实测 work_list 每条都带完整 author）。
        比猜一个 /user/info 接口靠谱得多，而且这一枪本来就要打（顺手当登录态探针）。
        取不到时返回 {}，调用方继续用 "acct:<id>" 兜底，不要瞎猜。
        """
        env = await self.fetch_work_page(max_cursor=0, count=1)
        if not env["__ok"]:
            return {}
        for w in env["works"]:
            a = w.get("author") or {}
            if a.get("uid") or a.get("sec_uid"):
                return a
        return {}

    # ==================== 评论 ====================
    async def fetch_comments(self, item_id, cursor=0, count=20, sort="TIME"):
        """一页一级评论。item_id 必须是加密串。

        返回 {"__ok", "comments":[...], "cursor", "has_more", "total_count"}。
        """
        env = await self._request("GET", api.comment_list_url(item_id, cursor=cursor,
                                                              count=count, sort=sort))
        if not env["__ok"]:
            return env
        rows, nxt, more, total = api.comments_of(env["raw"])
        return {"__ok": True, "comments": rows, "cursor": nxt,
                "has_more": more, "total_count": total, "raw": env["raw"]}

    async def fetch_replies(self, comment_id, item_id, cursor=0, count=20):
        env = await self._request("GET", api.reply_list_url(comment_id, item_id,
                                                            cursor=cursor, count=count))
        if not env["__ok"]:
            return env
        rows, nxt, more, total = api.comments_of(env["raw"])
        return {"__ok": True, "comments": rows, "cursor": nxt,
                "has_more": more, "total_count": total, "raw": env["raw"]}

    # ==================== 写操作（评论） ====================
    async def reply_comment(self, comment_id, item_id, text):
        """回复评论。🚨 body 键名 comment_Id（大写 I）由 api.reply_body 统一把关。"""
        try:
            body = api.comment_reply_body(comment_id, item_id, text)
        except ValueError as e:
            return {"__ok": False, "__err": "bad_args", "__msg": str(e)}
        return await self._request("POST", api.comment_reply_url(), body=body,
                                   is_write=True)

    async def delete_comment(self, comment_id, item_id):
        try:
            body = api.comment_delete_body(comment_id, item_id)
        except ValueError as e:
            return {"__ok": False, "__err": "bad_args", "__msg": str(e)}
        return await self._request("POST", api.comment_delete_url(), body=body,
                                   is_write=True)

    # ==================== 写操作（作品） ====================
    async def top_work(self, aweme_id, top=True):
        """置顶 / 取消置顶（表单编码）。"""
        try:
            body = api.work_top_body(aweme_id, top=top)
        except ValueError as e:
            return {"__ok": False, "__err": "bad_args", "__msg": str(e)}
        return await self._request("POST", api.work_top_url(), body=body, form=True,
                                   is_write=True)

    async def delete_work(self, aweme_id):
        """删除作品（表单编码，参数名 item_id）。"""
        try:
            body = api.work_delete_body(aweme_id)
        except ValueError as e:
            return {"__ok": False, "__err": "bad_args", "__msg": str(e)}
        return await self._request("POST", api.work_delete_url(), body=body, form=True,
                                   is_write=True)
