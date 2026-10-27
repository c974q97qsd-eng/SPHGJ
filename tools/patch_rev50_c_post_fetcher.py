# -*- coding: utf-8 -*-
"""rev50 步骤3: post_fetcher 三处改 HTTP 直连（P1）。

1. refresh()        —— 去掉 page.goto + wait_for_timeout(6000) 纯等待，改 httpx 翻页
2. find_video_url() —— 同上（原 goto + 3s）
3. _ensure_covers() —— context.request.get -> wx_http.get_bytes（CDN 公开，无需 cookie）

两条路都保留浏览器回退：HTTP 拿不到就整轮切浏览器，功能不会因为 httpx 不可用而挂掉。
"""
import io
import sys

P = r"D:\SPHGJ\backend\post_fetcher.py"

s = io.open(P, encoding="utf-8").read()
orig = len(s)

# ---------- 1) 文件头 + import ----------
OLD_HEAD = '''"""作品抓取器:官方「内容管理-视频」页 post_list -> 本地 posts 表 + 封面落盘 data/covers。'''
NEW_HEAD = '''"""作品抓取器:官方「内容管理-视频」页 post_list -> 本地 posts 表 + 封面落盘 data/covers。

rev50(2026-10-28): 主路径改**进程内 HTTP 直连**(backend/wx_http.py)。
  原实现为了拿到同源 cookie,必须先 new_page + goto(post/list) + wait 6 秒，
  而真正需要的只是"一次带 cookie 的 POST"。改成 httpx 后:
    - refresh()        省掉 goto + 6 秒纯等待，且不再新建页面
    - find_video_url() 省掉 goto + 3 秒
    - 封面下载         走 wx_http.get_bytes(CDN 公开，本来不需要 cookie)
  HTTP 不可用(无 storage_state / errCode!=0 / 网络错)时，自动回退原来的浏览器路径。
原说明如下 ——'''
assert s.count(OLD_HEAD) == 1, "文件头未命中"
s = s.replace(OLD_HEAD, NEW_HEAD, 1)

OLD_IMP = '''from .storage import cover_key

logger = logging.getLogger("sphgj")'''
NEW_IMP = '''from .storage import cover_key
from . import wx_http

logger = logging.getLogger("sphgj")'''
assert s.count(OLD_IMP) == 1, "import 段未命中"
s = s.replace(OLD_IMP, NEW_IMP, 1)

# ---------- 2) 公共 HTTP 取页方法 ----------
OLD_PROP = '''    @property
    def finder_id(self):
        """作品归属主键:视频号 ID。登录卡片 id 会变,视频号 ID 不会。"""
        return (self.account.get("_log_finder_id") or "").strip()'''
NEW_PROP = '''    @property
    def finder_id(self):
        """作品归属主键:视频号 ID。登录卡片 id 会变,视频号 ID 不会。"""
        return (self.account.get("_log_finder_id") or "").strip()

    # ==================== rev50: HTTP 直连 ====================

    def _latest_account(self):
        """现取 config 里的账号 —— 重新扫码后 storage_state / _aid 会被换掉。"""
        aid = self.account.get("id")
        if aid:
            for a in (self.config.get("accounts") or []):
                if a.get("id") == aid:
                    return a
        return self.account

    def _aid(self):
        return (self._latest_account().get("_aid") or self.account.get("_aid") or "").strip()

    async def _post_list_http(self, cur, page_size=None):
        """HTTP 取一页 post_list。返回 json dict;None = 走不通,请回退浏览器。"""
        acc = self._latest_account()
        aid = self._aid()
        if not aid:
            return None
        url = f"{BASE}/post/post_list?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
        body = wx_http.std_body(
            self.finder_id, pageSize=int(page_size or self.page_size), currentPage=cur,
            userpageType=11, stickyOrder=True)
        try:
            j = await wx_http.post_json(acc, url, body, page_url=PAGE_URL, timeout=30)
        except Exception as e:
            logger.debug(f"[posts] HTTP 取第 {cur} 页异常: {e}")
            return None
        if not isinstance(j, dict) or j.get("__err"):
            logger.debug(f"[posts] HTTP 取第 {cur} 页不可用: {j.get('__err') if isinstance(j, dict) else j}")
            return None
        if j.get("errCode") not in (0, None):
            logger.debug(f"[posts] HTTP 取第 {cur} 页 errCode={j.get('errCode')}")
            return None
        return j

    async def _post_list_page(self, page, cur, page_size=None):
        """浏览器路径取一页（原实现，保留作回退）。"""
        aidv = self._aid()
        url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
        r = await asyncio.wait_for(
            page.evaluate(FETCH_JS, {"url": url, "body": _body(
                self.finder_id, pageSize=int(page_size or self.page_size),
                currentPage=cur, userpageType=11, stickyOrder=True)}), timeout=30)
        try:
            return json.loads(r["body"])
        except Exception:
            return {"__parse_error": (r.get("body") or "")[:120]}'''
assert s.count(OLD_PROP) == 1, "_aid 属性段未命中"
s = s.replace(OLD_PROP, NEW_PROP, 1)

# ---------- 3) find_video_url 改 HTTP 优先 ----------
OLD_FIND_START = '''        ps = int(page_size or self.page_size)
        aidv = self.account.get("_aid") or ""
        page = None
        try:
            page = await worker.context.new_page()'''
NEW_FIND_START = '''        ps = int(page_size or self.page_size)
        # rev50: 先纯 HTTP 翻页找（不再为拿 cookie 先 goto 等 3 秒）
        http_ok = True
        for pg in range(1, int(max_pages) + 1):
            j = await self._post_list_http(pg, page_size=ps)
            if j is None:
                http_ok = False
                break
            lst = (((j.get("data") or {}).get("list")) or [])
            for p in lst:
                if (p.get("objectId") or p.get("exportId")) == oid:
                    ms = ((p.get("desc") or {}).get("media") or [])
                    if ms:
                        return ms[0].get("url") or ms[0].get("fullUrl") or ""
            if not lst:
                return ""
        if http_ok and not (worker is not None and worker.context is not None):
            return ""
        if worker is None or worker.context is None:
            return ""
        aidv = self._aid()
        page = None
        try:
            page = await worker.context.new_page()'''
assert s.count(OLD_FIND_START) == 1, "find_video_url 头部未命中"
s = s.replace(OLD_FIND_START, NEW_FIND_START, 1)

# ---------- 4) 封面下载改 wx_http ----------
OLD_COVER = '''                try:
                    resp = await context.request.get(cu, headers={"Referer": PAGE_URL}, timeout=20000)
                    if resp.status == 200:
                        b = await resp.body()

                        def _save():
                            with open(fpath, "wb") as f:
                                f.write(b)
                        await asyncio.to_thread(_save)
                        r["cover_path"] = f"/media/covers/{key}/{fname}"
                        return 1
                except Exception as e:
                    logger.debug(f"[{aid}] 封面下载失败: {e}")'''
NEW_COVER = '''                try:
                    # rev50: 走进程内 HTTP。CDN 图片是公开资源,不需要登录 cookie,
                    # 原实现借 context.request 纯粹是"手上正好有浏览器"。
                    b = await wx_http.get_bytes(cu, referer=PAGE_URL, timeout=20)
                    if b:
                        def _save():
                            with open(fpath, "wb") as f:
                                f.write(b)
                        await asyncio.to_thread(_save)
                        r["cover_path"] = f"/media/covers/{key}/{fname}"
                        return 1
                except Exception as e:
                    logger.debug(f"[{aid}] 封面下载失败: {e}")'''
assert s.count(OLD_COVER) == 1, "封面下载段未命中"
s = s.replace(OLD_COVER, NEW_COVER, 1)

# ---------- 5) refresh 主循环改 HTTP 优先 ----------
OLD_REFRESH = '''        context = worker.context
        if context is None:
            return {"error": "浏览器未打开"}
        aidv = self.account.get("_aid", "")
        if not aidv:
            return {"error": "缺少 _aid,请先启动引擎抓取一次"}

        page = await context.new_page()
        pages = 0
        fetched_total = 0
        covers = 0
        early_stop = False
        reached_end = False   # 是否已翻到最后一页(用于标记本地已全量)
        try:
            try:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.info(f"[{aid}] post/list goto: {e}")
            await page.wait_for_timeout(6000)

            for cur in range(1, self.max_pages + 1):
                url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
                try:
                    r = await asyncio.wait_for(
                        page.evaluate(FETCH_JS, {"url": url, "body": _body(
                            fid, pageSize=self.page_size, currentPage=cur,
                            userpageType=11, stickyOrder=True)}), timeout=30)
                except asyncio.TimeoutError:
                    return {"error": f"post_list 第 {cur} 页超时",
                            "pages": pages, "fetched": fetched_total, "covers": covers}
                try:
                    j = json.loads(r["body"])
                except Exception:
                    return {"error": f"post_list 第 {cur} 页解析失败: {r['body'][:120]}",
                            "pages": pages, "fetched": fetched_total, "covers": covers}
                if j.get("errCode") not in (0, None):'''

NEW_REFRESH = '''        context = worker.context if worker is not None else None
        # rev50: 不再强制要求 context —— 纯 HTTP 也能抓。browser 只在回退时惰性打开。
        if not self._aid():
            return {"error": "缺少 _aid,请先启动引擎抓取一次"}

        page = None            # 惰性创建,只有 HTTP 失败才会用到
        use_http = True
        pages = 0
        fetched_total = 0
        covers = 0
        early_stop = False
        reached_end = False   # 是否已翻到最后一页(用于标记本地已全量)

        async def _ensure_page():
            """HTTP 不可用时才建临时页并落到 channels 域（跨站 fetch 不带 cookie）。"""
            nonlocal page
            if page is not None:
                return page
            if context is None:
                return None
            page = await context.new_page()
            try:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.info(f"[{aid}] post/list goto: {e}")
            await page.wait_for_timeout(6000)
            return page

        try:
            for cur in range(1, self.max_pages + 1):
                j = None
                # 每页最多两跳:先 HTTP,不可用则本页改浏览器重取
                for _attempt in (0, 1):
                    if use_http:
                        j = await self._post_list_http(cur)
                        if j is None:
                            use_http = False
                            logger.info(f"[{aid}] 作品列表 HTTP 直连不可用,本轮回退浏览器")
                    if j is None:
                        p = await _ensure_page()
                        if p is None:
                            return {"error": "HTTP 与浏览器均不可用",
                                    "pages": pages, "fetched": fetched_total, "covers": covers}
                        try:
                            j = await self._post_list_page(p, cur)
                        except asyncio.TimeoutError:
                            return {"error": f"post_list 第 {cur} 页超时",
                                    "pages": pages, "fetched": fetched_total, "covers": covers}
                        if j.get("__parse_error") is not None:
                            return {"error": f"post_list 第 {cur} 页解析失败: {j['__parse_error']}",
                                    "pages": pages, "fetched": fetched_total, "covers": covers}
                    if j is not None:
                        break
                if j is None:
                    return {"error": f"post_list 第 {cur} 页取不到",
                            "pages": pages, "fetched": fetched_total, "covers": covers}
                if j.get("errCode") not in (0, None):'''
assert s.count(OLD_REFRESH) == 1, "refresh 主循环未命中"
s = s.replace(OLD_REFRESH, NEW_REFRESH, 1)

# ---------- 6) finally 里 page 可能仍是 None ----------
OLD_FIN = '''        finally:
            try:
                await page.close()
            except Exception:
                pass'''
NEW_FIN = '''        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass'''
assert s.count(OLD_FIN) == 1, "finally 段未命中"
s = s.replace(OLD_FIN, NEW_FIN, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------- 自校验 ----------
chk = io.open(P, encoding="utf-8").read()
must = [
    "from . import wx_http",
    "async def _post_list_http(",
    "async def _post_list_page(",
    "def _latest_account(",
    "wx_http.get_bytes(cu, referer=PAGE_URL",
    "await page.goto(POST_PAGE_URL" if False else "use_http = True",
]
must_not = [
    "resp = await context.request.get(cu",
    'aidv = self.account.get("_aid", "")',
]
bad = [m for m in must if m not in chk] + [m for m in must_not if m in chk]
if bad:
    print("SELFCHECK FAIL:", bad)
    sys.exit(1)
print("OK  %d -> %d 字节 (+%d)" % (orig, len(chk), len(chk) - orig))
print("SELFCHECK PASS")
