# -*- coding: utf-8 -*-
"""rev65: 作品播放链路收敛为纯 HTTP + 页码直跳（一次性补丁，含写后自校验）。

实测依据（母本某账号, 2026-11-06）:
  - 官方 post_list 传 objectId / exportId / objectIdList 都被忽略,仍按页返回 200 条
  - 该账号 total=2587, 单页 1.4s; 老作品顺序翻到第 12 页 ≈ 17s
  - 库内按 create_time 倒序排名算 currentPage, 估算第 12 页一次命中(1.42s)
改动:
  1) post_fetcher 新增 _guess_start_page(), find_video_url_http 改为候选页直跳
  2) server /api/posts/play 去掉同步 CDN 探测 + 浏览器兜底, 纯 HTTP + 8s 总时限
"""
import ast
import io
import os
import sys

ROOT = r"D:\SPHGJ"
PF = os.path.join(ROOT, "backend", "post_fetcher.py")
SV = os.path.join(ROOT, "backend", "server.py")

PF_START = "    async def find_video_url_http(self, object_id, max_pages=None):"
PF_END = "    async def find_video_url(self, worker, object_id, max_pages=3, page_size=None):"
SV_START = '@app.get("/api/posts/play")'
SV_END = '@app.websocket("/api/remote/ws")'

PF_NEW = '''    def _guess_start_page(self, object_id, page_size):
        """rev65: 按库内 create_time 倒序排名估算官方列表页码。

        官方 post_list 不支持按 objectId 过滤(实测 objectId/exportId/objectIdList
        全被忽略,仍返回该页 200 条)。老作品从第 1 页顺序翻要 12+ 页 ≈ 17s。
        库里存着全部作品的 create_time,一条 COUNT 就能算出目标大约在第几页;
        实测 2639 条库内作品 / 2587 官方作品,估算第 12 页一次命中。
        估算不出来(老库 / stub / 异常)返回 1,退化为原来的顺序翻页。
        """
        try:
            ps = max(1, int(page_size))
            c = self.storage._conn()
            try:
                r = c.execute(
                    "SELECT create_time FROM posts WHERE finder_id=? AND object_id=?",
                    (self.finder_id, object_id)).fetchone()
                if not r or not r[0]:
                    return 1
                n = c.execute(
                    "SELECT COUNT(*) FROM posts WHERE finder_id=? AND create_time>?",
                    (self.finder_id, r[0])).fetchone()[0]
            finally:
                c.close()
            return max(1, n // ps + 1)
        except Exception:
            return 1

    async def find_video_url_http(self, object_id, max_pages=None):
        """rev65: 纯 HTTP 现取播放地址 —— 播放链路唯一的取地址入口。

        按库内时间排名直跳页码(前后各 2 页容错),不再从第 1 页顺序翻。
        返回 (url, why):url 为空时 why 是失败原因,供上层给出准确提示。
        """
        oid = (object_id or "").strip()
        if not oid:
            return "", "empty_object_id"
        acc = self._latest_account()
        aid = (acc.get("_aid") or self.account.get("_aid") or "").strip()
        if not aid:
            return "", "no_aid"
        if not wx_http.cookie_header_of(acc):
            return "", "no_cookie"
        pc = self.config.get("posts") or {}
        limit = max(1, int(max_pages or pc.get("play_max_pages", 20)))
        ps = int(self.page_size or 200)
        guess = min(max(1, self._guess_start_page(oid, ps)), limit)
        # 候选页:估算页 -> 第 1 页(置顶作品永远在这) -> 前后各 2 页 -> 其余页顺序补齐。
        # 置顶作品 create_time 很老,按时间估算会指向列表末尾甚至超界;官方
        # stickyOrder=True 把它排在第 1 页,所以第 1 页必须固定进候选。
        order, seen = [], set()
        for pg in (guess, 1, guess + 1, guess - 1, guess + 2, guess - 2):
            if 1 <= pg <= limit and pg not in seen:
                seen.add(pg)
                order.append(pg)
        near = set(order)       # 容错页不受"到尾/空页"早停影响(估算超界会返回空页)
        for pg in range(1, limit + 1):
            if pg not in seen:
                seen.add(pg)
                order.append(pg)
        empty = 0
        for pg in order:
            self._last_post_list_why = ""
            j = await self._post_list_http(pg, page_size=ps)
            if j is None:
                return "", self._last_post_list_why or "post_list_unavailable"
            d = j.get("data") or {}
            lst = d.get("list") or []
            if not lst:
                if pg not in near:      # 估算页超界时的空页不代表列表到底
                    empty += 1
                    if empty >= 2:      # 补齐阶段连续两页空才算到底
                        return "", "not_in_list"
                continue
            empty = 0
            for p in lst:
                if (p.get("objectId") or p.get("exportId")) == oid:
                    ms = ((p.get("desc") or {}).get("media") or [])
                    u = (ms[0].get("url") or ms[0].get("fullUrl") or "") if ms else ""
                    return (u, "") if u else ("", "no_media_url")
            total = int(d.get("total") or d.get("totalCount") or 0)
            tail = len(lst) < ps or (total > 0 and pg * ps >= total)
            if tail and pg not in near:
                return "", "not_in_list"
        return "", "page_limit"

'''

SV_NEW = '''@app.get("/api/posts/play")
async def post_play(finder_id: str = Query(""), object_id: str = Query("")):
    """取作品播放地址 —— rev65 全程纯 HTTP,零浏览器、零 CDN 预探测。

    旧链路:先同步 Range 探测旧 CDN 地址(8s) -> 失效再翻 post_list -> 再探测 ->
    失败还要开浏览器兜底(goto 45s + 固定 3s)。老作品翻 12 页要等十几秒,
    前端又是裸 fetch 无超时,于是「加载中…」一直转圈。
    现在:按作品在库内的时间排名直跳官方页码,一次 HTTP 拿最新地址;
    整条链路 8s 上限,拿不到就给准确原因,绝不开浏览器。
    """
    if not finder_id or not object_id:
        raise HTTPException(400, "缺少 finder_id / object_id")
    row = storage.get_post_video(finder_id, object_id)
    if row is None:
        raise HTTPException(404, "作品不存在")
    title = row.get("title") or ""
    acc = _account_for_finder(finder_id)
    if acc is None:
        return {"ok": False, "title": title,
                "message": _play_fail_message("no_account_for_finder")}
    try:
        fresh, why = await asyncio.wait_for(
            PostFetcher(storage, acc, config).find_video_url_http(object_id),
            timeout=8.0)
    except asyncio.TimeoutError:
        return {"ok": False, "title": title,
                "message": "取播放地址超时(8 秒),视频号接口响应慢,请稍后重试。"}
    except Exception as e:
        return {"ok": False, "title": title,
                "message": f"取播放地址异常({type(e).__name__}),请稍后重试。"}
    if not fresh:
        return {"ok": False, "title": title, "message": _play_fail_message(why)}
    try:
        storage.update_post_video(finder_id, object_id, fresh)
    except Exception as e:
        logger.warning(f"[posts] 写回播放地址失败(不影响本次播放): {e}")
    return {"ok": True, "url": fresh, "refreshed": True, "title": title}


'''


def slice_replace(s, start, end, new, tag):
    assert s.count(start) == 1, tag + ": 起始标记不唯一"
    assert s.count(end) == 1, tag + ": 结束标记不唯一"
    a = s.index(start)
    b = s.index(end, a)
    assert a < b, tag + ": 标记顺序异常"
    return s[:a] + new + s[b:]


def main():
    pf = io.open(PF, encoding="utf-8", newline="").read()
    sv = io.open(SV, encoding="utf-8", newline="").read()
    assert "rev65" not in pf and "rev65" not in sv, "已经打过 rev65,别重复执行"

    pf = slice_replace(pf, PF_START, PF_END, PF_NEW, "post_fetcher")
    sv = slice_replace(sv, SV_START, SV_END, SV_NEW, "server")

    # 语法检查后再落盘
    ast.parse(pf)
    ast.parse(sv)
    io.open(PF, "w", encoding="utf-8", newline="").write(pf)
    io.open(SV, "w", encoding="utf-8", newline="").write(sv)

    # 写后自校验:重新读盘,逐条断言
    pf2 = io.open(PF, encoding="utf-8", newline="").read()
    sv2 = io.open(SV, encoding="utf-8", newline="").read()
    checks = [
        ("pf 新增页码估算", "def _guess_start_page" in pf2),
        ("pf 直跳候选页", "order, seen = [], set()" in pf2),
        ("pf 用候选页请求", "await self._post_list_http(pg, page_size=ps)" in pf2),
        ("pf 空页容错", "if empty >= 2:" in pf2),
        ("pf 保留浏览器兜底函数", "async def find_video_url(self, worker" in pf2),
        ("sv 纯 HTTP 一次调用", "find_video_url_http(object_id)" in sv2),
        ("sv 8 秒总时限", "timeout=8.0" in sv2),
        ("sv 无同步 CDN 探测", "_url_playable" not in sv2.split(SV_START)[1].split(SV_END)[0]),
        ("sv 无浏览器兜底", "find_video_url(w, object_id)" not in sv2.split(SV_START)[1].split(SV_END)[0]),
        ("sv 写回地址", "storage.update_post_video(finder_id, object_id, fresh)" in sv2),
        ("sv 保留错误原因映射", "_play_fail_message(why)" in sv2),
    ]
    ok = True
    for name, cond in checks:
        print("  %s %s" % ("OK " if cond else "BAD", name))
        ok = ok and cond
    print("SELFCHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
