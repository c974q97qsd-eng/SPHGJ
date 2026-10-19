# -*- coding: utf-8 -*-
"""rev27b: 作品可播放(存视频地址 + 失效自动续签)。"""
import io
import os

LOCAL = r"D:\SPHGJ"
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# ---------------- storage ----------------
add(r"backend\storage.py",
    """            visible_type INTEGER, sticky_op INTEGER, stat_sig TEXT,
            raw TEXT, updated_at TEXT,
            PRIMARY KEY(finder_id, object_id))\"\"\"""",
    """            visible_type INTEGER, sticky_op INTEGER, stat_sig TEXT,
            video_url TEXT,
            raw TEXT, updated_at TEXT,
            PRIMARY KEY(finder_id, object_id))\"\"\"""",
    "posts DDL 加 video_url")

add(r"backend\storage.py",
    """        c.execute(POSTS_DDL)
        c.execute(META_DDL)""",
    """        c.execute(POSTS_DDL)
        # rev27: 作品播放地址(CDN 直链,带时效;失效时由 /api/posts/play 现取)
        _pcols = {r[1] for r in c.execute("PRAGMA table_info(posts)")}
        if "video_url" not in _pcols:
            c.execute("ALTER TABLE posts ADD COLUMN video_url TEXT")
        c.execute(META_DDL)""",
    "老库补 video_url 列")

add(r"backend\storage.py",
    """                c.execute(\"\"\"INSERT INTO posts(finder_id,object_id,account_id,export_id,title,cover_url,
                             cover_hash,cover_path,create_time,read_count,like_count,comment_count,
                             forward_count,fav_count,follow_count,visible_type,sticky_op,stat_sig,raw,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(finder_id,object_id) DO UPDATE SET
                      account_id=excluded.account_id,
                      export_id=excluded.export_id, title=excluded.title,
                      cover_url=excluded.cover_url, cover_hash=excluded.cover_hash, cover_path=excluded.cover_path,
                      create_time=excluded.create_time, read_count=excluded.read_count, like_count=excluded.like_count,
                      comment_count=excluded.comment_count, forward_count=excluded.forward_count,
                      fav_count=excluded.fav_count, follow_count=excluded.follow_count,
                      visible_type=excluded.visible_type, sticky_op=excluded.sticky_op,
                      stat_sig=excluded.stat_sig, raw=excluded.raw, updated_at=excluded.updated_at\"\"\",
                    (finder_id, r["object_id"], account_id or "", r["export_id"], r["title"],
                     r["cover_url"], r["cover_hash"], r.get("cover_path") or "",
                     r["create_time"], r["read_count"], r["like_count"], r["comment_count"],
                     r["forward_count"], r["fav_count"], r["follow_count"],
                     r["visible_type"], r["sticky_op"], sig, r["raw"], now))""",
    """                c.execute(\"\"\"INSERT INTO posts(finder_id,object_id,account_id,export_id,title,cover_url,
                             cover_hash,cover_path,create_time,read_count,like_count,comment_count,
                             forward_count,fav_count,follow_count,visible_type,sticky_op,stat_sig,
                             video_url,raw,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(finder_id,object_id) DO UPDATE SET
                      account_id=excluded.account_id,
                      export_id=excluded.export_id, title=excluded.title,
                      cover_url=excluded.cover_url, cover_hash=excluded.cover_hash, cover_path=excluded.cover_path,
                      create_time=excluded.create_time, read_count=excluded.read_count, like_count=excluded.like_count,
                      comment_count=excluded.comment_count, forward_count=excluded.forward_count,
                      fav_count=excluded.fav_count, follow_count=excluded.follow_count,
                      visible_type=excluded.visible_type, sticky_op=excluded.sticky_op,
                      stat_sig=excluded.stat_sig,
                      video_url=CASE WHEN COALESCE(excluded.video_url,'')<>''
                                     THEN excluded.video_url ELSE posts.video_url END,
                      raw=excluded.raw, updated_at=excluded.updated_at\"\"\",
                    (finder_id, r["object_id"], account_id or "", r["export_id"], r["title"],
                     r["cover_url"], r["cover_hash"], r.get("cover_path") or "",
                     r["create_time"], r["read_count"], r["like_count"], r["comment_count"],
                     r["forward_count"], r["fav_count"], r["follow_count"],
                     r["visible_type"], r["sticky_op"], sig, r.get("video_url") or "",
                     r["raw"], now))""",
    "upsert_posts 写 video_url")

# 回填 + 取/改地址
add(r"backend\storage.py",
    '''    def posts_finders(self):''',
    '''    def backfill_video_urls(self, limit=100000):
        """从已存的 raw 里补 video_url(rev27,幂等)。

        历史行没有单独存播放地址,但 raw 里有完整 desc.media[0].url —— 不用重新抓。
        注意 CDN 地址带时效:补出来的是「当时抓取那一刻」的地址,过期后由
        /api/posts/play 现取新的。
        """
        c = self._conn()
        n = 0
        try:
            rows = c.execute("SELECT finder_id,object_id,raw FROM posts "
                             "WHERE COALESCE(video_url,'')='' AND raw LIKE '%\"url\"%' "
                             "LIMIT ?", (int(limit),)).fetchall()
            for fid, oid, raw in rows:
                url = ""
                try:
                    p = json.loads(raw or "{}")
                    ms = ((p.get("desc") or {}).get("media") or [])
                    if ms:
                        url = ms[0].get("url") or ms[0].get("fullUrl") or ""
                except Exception:
                    url = ""
                if url:
                    c.execute("UPDATE posts SET video_url=? WHERE finder_id=? AND object_id=?",
                              (url, fid, oid))
                    n += 1
            c.commit()
        finally:
            c.close()
        return n

    def get_post_video(self, finder_id, object_id):
        """取一条作品的播放信息(地址 + 标题)。"""
        c = self._conn()
        try:
            r = c.execute("SELECT title,video_url,create_time FROM posts "
                          "WHERE finder_id=? AND object_id=?", (finder_id, object_id)).fetchone()
            if not r:
                return None
            return {"title": r[0] or "", "video_url": r[1] or "", "create_time": r[2]}
        finally:
            c.close()

    def update_post_video(self, finder_id, object_id, url):
        c = self._conn()
        try:
            c.execute("UPDATE posts SET video_url=? WHERE finder_id=? AND object_id=?",
                      (url or "", finder_id, object_id))
            c.commit()
        finally:
            c.close()

    def posts_finders(self):''',
    "回填与取改视频地址")

# ---------------- post_fetcher ----------------
add(r"backend\post_fetcher.py",
    '''        "cover_url": cover_url,''',
    '''        "cover_url": cover_url,
        "video_url": media.get("url") or media.get("fullUrl") or "",''',
    "抓取时存视频地址")

add(r"backend\post_fetcher.py",
    '''    # ---------------- 封面 ----------------''',
    '''    async def find_video_url(self, worker, object_id, max_pages=3, page_size=None):
        """现取某作品的播放地址(CDN 地址有时效,旧作品存的会过期)。

        复用 worker 的 context 开临时页,直接打 post_list 接口翻前几页找 object_id。
        找不到返回 ""。
        """
        oid = (object_id or "").strip()
        if not oid or worker is None or worker.context is None:
            return ""
        ps = int(page_size or self.page_size)
        aidv = self.account.get("_aid") or ""
        page = None
        try:
            page = await worker.context.new_page()
            for pg in range(1, int(max_pages) + 1):
                url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
                r = await page.evaluate(FETCH_JS, {"url": url, "body": _body(
                    self.finder_id, pageSize=ps, currentPage=pg)})
                try:
                    data = json.loads(r.get("body") or "{}")
                except Exception:
                    continue
                lst = (((data.get("data") or {}).get("list")) or [])
                for p in lst:
                    if (p.get("objectId") or p.get("exportId")) == oid:
                        ms = ((p.get("desc") or {}).get("media") or [])
                        if ms:
                            return ms[0].get("url") or ms[0].get("fullUrl") or ""
                if not lst:
                    break
        except Exception as e:
            logger.debug(f"[posts] 现取视频地址失败 {oid}: {e}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
        return ""

    # ---------------- 封面 ----------------''',
    "find_video_url")

# ---------------- server: 播放接口 ----------------
add(r"backend\server.py",
    """import socket
import time
import random""",
    """import socket
import time
import random
import urllib.request""",
    "server 导入 urllib")

add(r"backend\server.py",
    '''@app.websocket("/api/remote/ws")''',
    '''def _url_playable(url, timeout=8):
    """CDN 地址是否还能拉(带 Range 只取 2 字节,便宜)。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except Exception:
        return False


@app.get("/api/posts/play")
async def post_play(finder_id: str = Query(""), object_id: str = Query("")):
    """取作品播放地址。存的地址过期时用该视频号的浏览器现取一次。

    CDN 直链带时效(实测 2025 年的地址已 400),所以不能只存一次就完事。
    """
    if not finder_id or not object_id:
        raise HTTPException(400, "缺少 finder_id / object_id")
    row = storage.get_post_video(finder_id, object_id)
    if row is None:
        raise HTTPException(404, "作品不存在")
    url = row.get("video_url") or ""
    refreshed = False
    if not await asyncio.to_thread(_url_playable, url):
        # 地址为空或已过期 -> 用登录着该视频号的 worker 现取
        aid = ""
        for a_id, w in (manager.workers or {}).items():
            if (w.account.get("_log_finder_id") or "").strip() == finder_id:
                aid = a_id
                break
        if not aid:
            acc = next((a for a in config.get("accounts", [])
                        if (a.get("_log_finder_id") or a.get("locked_finder_id") or "") == finder_id),
                       None)
            aid = (acc or {}).get("id") or ""
        w = (manager.workers or {}).get(aid)
        if w is not None and w.logged_in:
            acc_obj = w.account
            fresh = await PostFetcher(storage, acc_obj, config).find_video_url(w, object_id)
            if fresh:
                storage.update_post_video(finder_id, object_id, fresh)
                url, refreshed = fresh, True
    if not await asyncio.to_thread(_url_playable, url):
        return {"ok": False, "title": row.get("title") or "",
                "message": "这条作品的播放地址已失效,且当前无法现取"
                           "(视频号未运行/作品太旧不在列表前几页)。"
                           "启动该账号后重试,或到视频号后台查看。"}
    return {"ok": True, "url": url, "refreshed": refreshed,
            "title": row.get("title") or ""}


@app.websocket("/api/remote/ws")''',
    "播放接口")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")

    print("\n== 自校验 ==")
    import ast
    for f in ("storage", "post_fetcher", "server"):
        ast.parse(io.open(os.path.join(LOCAL, "backend", f + ".py"), encoding="utf-8").read())
        print(f"  OK  AST {f}.py")
    for rel, needle, tag in (
            (r"backend\storage.py", "def backfill_video_urls", "回填"),
            (r"backend\storage.py", "def get_post_video", "读取"),
            (r"backend\post_fetcher.py", "async def find_video_url", "现取"),
            (r"backend\server.py", '"/api/posts/play"', "播放接口"),
            (r"backend\server.py", "import urllib.request", "urllib 导入")):
        s = io.open(os.path.join(LOCAL, rel), encoding="utf-8").read()
        print(f"  {'OK ' if needle in s else 'BAD'} {tag}")
    print("\n[DONE]")


if __name__ == "__main__":
    apply()
