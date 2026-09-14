"""作品抓取器:官方「内容管理-视频」页 post_list -> 本地 posts 表 + 封面落盘 data/covers。

接口(与官方前端一致,已在 2026-09-15 探测实锤):
  POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_list
       ?_aid=..&_pageUrl=https://channels.weixin.qq.com/platform/post/list
       body: {pageSize, currentPage, userpageType:11, stickyOrder:true, +公共字段}
  封面: context.request.get(coverUrl, Referer=platform/post/list) —— CDN GET,不占 API 配额

增量闸门(压总请求量,任一命中即停翻页):
  1. pageSize=200 大页(2534 作品全量仅 13 页)
  2. 早停:整页作品均已入库且 stat_sig(播放/赞/评签名)未变 -> 立即停
  3. 页数上限 max_pages(默认 20)
  4. 刷新冷却 cooldown_sec(默认 60s,距上次刷新不足则直接跳过)
封面判重: md5(cover_url),文件在且 hash 未变 -> 一次请求都不发。

所有状态写 storage;请求量记 post_fetch_meta 账本(今日请求次数可见)。
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger("sphgj")

PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"
BASE = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin"
FETCH_JS = r"""async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify(body)
  });
  const t = await r.text();
  return {status: r.status, body: t.slice(0, 4000000)};
}"""


def _body(fid, **extra):
    b = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": fid,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    b.update(extra)
    return b


def _extract_post(p):
    """官方作品对象 -> 本表行字段。"""
    desc = p.get("desc") or {}
    media_list = desc.get("media") or []
    media = media_list[0] if media_list else {}
    cover_url = media.get("coverUrl") or media.get("thumbUrl") or ""
    return {
        "object_id": p.get("objectId") or p.get("exportId") or "",
        "export_id": p.get("exportId") or p.get("objectId") or "",
        "title": (desc.get("description") or "").strip(),
        "cover_url": cover_url,
        "cover_hash": hashlib.md5(cover_url.encode("utf-8")).hexdigest() if cover_url else "",
        "create_time": int(p.get("createTime") or 0),
        "read_count": int(p.get("readCount") or 0),
        "like_count": int(p.get("likeCount") or 0),
        "comment_count": int(p.get("commentCount") or 0),
        "forward_count": int(p.get("forwardCount") or 0),
        "fav_count": int(p.get("favCount") or 0),
        "follow_count": int(p.get("followCount") or 0),
        # visibleType: 1=公开 2=仅粉丝 3=仅自己可见;stickyOpStatus: 0=不可操作 1=可置顶 2=已置顶
        "visible_type": int(p.get("visibleType") or 0),
        "sticky_op": int(p.get("stickyOpStatus") or 0),
        "raw": json.dumps(p, ensure_ascii=False),
    }


class PostFetcher:
    """单账号作品抓取。复用 worker 的浏览器 context(开一个临时页,用完即关)。"""

    def __init__(self, storage, account, config=None):
        self.storage = storage
        self.account = account
        self.config = config or {}
        pc = self.config.get("posts") or {}
        self.max_pages = int(pc.get("max_pages", 20))
        self.cooldown_sec = int(pc.get("refresh_cooldown_sec", 60))
        self.page_size = int(pc.get("page_size", 200))
        data_dir = os.path.dirname(storage.db_path) if getattr(storage, "db_path", "") else "data"
        self.covers_root = os.path.join(data_dir, "covers")

    # ---------------- 封面 ----------------
    async def _ensure_covers(self, context, rows):
        """下载缺失/变更的封面(md5 判重,已有即零请求)。返回下载数。"""
        aid = self.account["id"]
        out_dir = os.path.join(self.covers_root, aid)
        os.makedirs(out_dir, exist_ok=True)
        todo = []
        for r in rows:
            cu = r["cover_url"]
            if not cu or not r["cover_hash"]:
                continue
            fname = r["cover_hash"] + ".jpg"
            fpath = os.path.join(out_dir, fname)
            exists = await asyncio.to_thread(os.path.isfile, fpath)
            if exists and await asyncio.to_thread(os.path.getsize, fpath) > 0:
                r["cover_path"] = f"/media/covers/{aid}/{fname}"
                continue
            todo.append((r, cu, fpath, fname))
        if not todo:
            return 0
        sem = asyncio.Semaphore(4)

        async def one(item):
            r, cu, fpath, fname = item
            async with sem:
                try:
                    resp = await context.request.get(cu, headers={"Referer": PAGE_URL}, timeout=20000)
                    if resp.status == 200:
                        b = await resp.body()

                        def _save():
                            with open(fpath, "wb") as f:
                                f.write(b)
                        await asyncio.to_thread(_save)
                        r["cover_path"] = f"/media/covers/{aid}/{fname}"
                        return 1
                except Exception as e:
                    logger.debug(f"[{aid}] 封面下载失败: {e}")
            return 0

        n = 0
        for i in range(0, len(todo), 8):
            chunk = todo[i:i + 8]
            res = await asyncio.gather(*(one(x) for x in chunk))
            n += sum(res)
            await asyncio.sleep(0.3)
        return n

    # ---------------- 主流程 ----------------
    async def refresh(self, worker):
        """抓取一个账号的全部作品(增量)。worker: AccountWorker(需已 ensure_browser)。"""
        aid = self.account["id"]
        meta = self.storage.get_post_fetch_meta(aid)
        if meta.get("last_refresh"):
            try:
                last = datetime.fromisoformat(meta["last_refresh"]).timestamp()
                if time.time() - last < self.cooldown_sec:
                    return {"skipped": f"冷却中({self.cooldown_sec}s 内已刷新)"}
            except Exception:
                pass

        context = worker.context
        if context is None:
            return {"error": "浏览器未打开"}
        fid = self.account.get("_log_finder_id", "")
        aidv = self.account.get("_aid", "")
        if not fid or not aidv:
            return {"error": "缺少 _aid/_log_finder_id,请先启动引擎抓取一次"}

        page = await context.new_page()
        pages = 0
        fetched_total = 0
        covers = 0
        early_stop = False
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
                if j.get("errCode") not in (0, None):
                    return {"error": f"post_list errCode={j.get('errCode')}",
                            "pages": pages, "fetched": fetched_total, "covers": covers}
                data = j.get("data") or {}
                lst = data.get("list") or []
                if not lst:
                    break
                rows = [_extract_post(p) for p in lst]
                # 封面判重 + 下载(先算 cover_path 再入库)
                covers += await self._ensure_covers(context, rows)
                await asyncio.to_thread(self.storage.upsert_posts, aid, rows)
                pages += 1
                fetched_total += len(rows)
                # 早停:整页已入库且统计签名未变 -> 后面都是老数据
                if await asyncio.to_thread(
                        self.storage.posts_known_and_unchanged, aid,
                        [(r["object_id"], f"{r['read_count']}:{r['like_count']}:{r['comment_count']}")
                         for r in rows]):
                    early_stop = True
                    break
                if len(lst) < self.page_size:
                    break
                await asyncio.sleep(1.2)  # 翻页间隔,防风控
            return {"pages": pages, "fetched": fetched_total, "covers": covers,
                    "early_stop": early_stop}
        finally:
            try:
                await page.close()
            except Exception:
                pass
