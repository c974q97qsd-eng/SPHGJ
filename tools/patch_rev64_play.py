# -*- coding: utf-8 -*-
"""rev64: 播放地址失效后用【当前账号的 HTTP 登录态】现取，不再依赖 worker/浏览器。

根因：纯 HTTP 模式账号(uspoloassn2 等)没有 worker.context，
      find_video_url() 开头 `worker.context is None -> return ""` 直接放弃，
      /api/posts/play 于是误报「视频号未运行」。实测同一账号 HTTP 打 post_list
      拿到的新地址 Range=206 可播（旧地址 400）。

改法：
  1) post_fetcher 新增 find_video_url_http()：按账号 cookie+_aid 翻 post_list，
     命中 object_id 就返回完整 media url；不依赖 worker/context。
  2) find_video_url() 改为 HTTP 优先，HTTP 走不通才回退原浏览器路径。
  3) server /api/posts/play 按 finder_id 从 config 取账号直接调 HTTP，
     写库前确认新地址可播；失败提示按真实原因区分，不再误导「未运行」。

同一文件多处修改 —— 一次性整文件读入、逐条断言唯一命中、一次写回、立刻自校验。
"""
import ast
import io
import os

ROOT = r"D:\SPHGJ"
PF = os.path.join(ROOT, "backend", "post_fetcher.py")
SV = os.path.join(ROOT, "backend", "server.py")

PATCHES = [
    # ---------------- post_fetcher: 状态位 ----------------
    (PF, "        self.covers_root = os.path.join(data_dir, \"covers\")\n",
     "        self.covers_root = os.path.join(data_dir, \"covers\")\n"
     "        self._last_post_list_why = \"\"   # rev64: _post_list_http 失败原因\n"
     "        self.last_why = \"\"              # rev64: find_video_url 最后一次失败原因\n",
     "pf 初始化状态位"),

    # ---------------- post_fetcher: _post_list_http 记录失败原因 ----------------
    (PF, "        aid = self._aid()\n        if not aid:\n            return None\n",
     "        aid = self._aid()\n        if not aid:\n"
     "            self._last_post_list_why = \"no_aid\"\n            return None\n",
     "pf no_aid"),

    (PF, "        except Exception as e:\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页异常: {e}\")\n            return None\n"
     "        if not isinstance(j, dict) or j.get(\"__err\"):\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页不可用: {j.get('__err') if isinstance(j, dict) else j}\")\n"
     "            return None\n"
     "        if j.get(\"errCode\") not in (0, None):\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页 errCode={j.get('errCode')}\")\n"
     "            return None\n"
     "        return j\n",
     "        except Exception as e:\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页异常: {e}\")\n"
     "            self._last_post_list_why = \"request_error:\" + type(e).__name__\n"
     "            return None\n"
     "        if not isinstance(j, dict) or j.get(\"__err\"):\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页不可用: {j.get('__err') if isinstance(j, dict) else j}\")\n"
     "            self._last_post_list_why = \"http:\" + str(j.get(\"__err\") if isinstance(j, dict) else \"bad_json\")\n"
     "            return None\n"
     "        if j.get(\"errCode\") not in (0, None):\n"
     "            logger.debug(f\"[posts] HTTP 取第 {cur} 页 errCode={j.get('errCode')}\")\n"
     "            self._last_post_list_why = \"errCode:\" + str(j.get(\"errCode\"))\n"
     "            return None\n"
     "        self._last_post_list_why = \"\"\n"
     "        return j\n",
     "pf 失败原因记录"),
]

NEW_FIND = '''    async def find_video_url_http(self, object_id, max_pages=None):
        """rev64: 纯 HTTP 现取播放地址,不依赖 worker / 浏览器 context。

        账号取自 config(按作品 finder_id 归属解析),用 wx_http 的 cookie + _aid
        直打 post_list,翻到命中 object_id 为止。返回 (url, why):
        url 为空时 why 是失败原因,供上层给出准确提示。
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
        for pg in range(1, limit + 1):
            self._last_post_list_why = ""
            j = await self._post_list_http(pg, page_size=ps)
            if j is None:
                return "", self._last_post_list_why or "post_list_unavailable"
            d = j.get("data") or {}
            lst = d.get("list") or []
            for p in lst:
                if (p.get("objectId") or p.get("exportId")) == oid:
                    ms = ((p.get("desc") or {}).get("media") or [])
                    u = (ms[0].get("url") or ms[0].get("fullUrl") or "") if ms else ""
                    return (u, "") if u else ("", "no_media_url")
            if not lst:
                return "", "not_in_list"
            total = int(d.get("total") or d.get("totalCount") or 0)
            if total > 0 and pg * ps >= total:
                return "", "not_in_list"
        return "", "page_limit"

    async def find_video_url(self, worker, object_id, max_pages=3, page_size=None):
        """现取某作品的播放地址(CDN 地址有时效,旧作品存的会过期)。

        rev64: 先纯 HTTP —— 纯 HTTP 模式的账号没有浏览器 context,原实现开头
        `worker.context is None -> return ""` 直接放弃,账号明明在跑却被误报
        「视频号未运行」。HTTP 走不通才回退浏览器路径(需要 context)。
        """
        oid = (object_id or "").strip()
        if not oid:
            return ""
        fresh, why = await self.find_video_url_http(object_id)
        self.last_why = why
        if fresh:
            return fresh
        if worker is None or worker.context is None:
            return ""
        ps = int(page_size or self.page_size)
        aidv = self._aid()
        page = None
        try:
            page = await worker.context.new_page()
            # 先落到 channels 域:new_page() 是 about:blank,跨站 fetch 不会带登录 cookie
            try:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.debug(f"[posts] 现取地址前打开列表页失败(继续): {e}")
            for pg in range(1, int(max_pages) + 1):
                url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
                r = await page.evaluate(FETCH_JS, {"url": url, "body": _body(
                    self.finder_id, pageSize=ps, currentPage=pg,
                    userpageType=11, stickyOrder=True)})
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

'''

NEW_PLAY = '''def _url_playable(url, timeout=8):
    """CDN 地址是否还能拉(带 Range 只取 2 字节,便宜)。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except Exception:
        return False


def _account_for_finder(finder_id):
    """rev64: 按作品归属视频号找账号 —— 从 config 全量取,不要求账号已启动 worker。

    归属主键是 finder_id。纯 HTTP 模式的账号没有 worker.context,但 config 里的
    storage_state / _aid 依然是最新的登录态,足以直打 post_list。
    """
    fid = (finder_id or "").strip()
    if not fid:
        return None
    for a in config.get("accounts", []):
        if (a.get("_log_finder_id") or a.get("locked_finder_id") or "").strip() == fid:
            return a
    for w in (manager.workers or {}).values():
        acc = getattr(w, "account", None) or {}
        if (acc.get("_log_finder_id") or acc.get("locked_finder_id") or "").strip() == fid:
            return acc
    return None


_PLAY_FAIL_MSG = {
    "no_account_for_finder": "找不到这条作品所属的视频号账号,无法自动取新地址。",
    "no_aid": "该账号缺少 _aid(登录信息不完整),请重新扫码登录后再试。",
    "no_cookie": "该账号的登录态(cookie)未保存或已清空,请重新扫码登录后再试。",
    "not_in_list": "该作品已不在视频号作品列表中(可能已删除或下架),请到视频号后台查看。",
    "page_limit": "在作品列表翻页上限内没找到这条作品,请到视频号后台查看。",
    "no_media_url": "作品列表里这条作品没有返回播放地址,请到视频号后台查看。",
    "empty_object_id": "作品 ID 为空。",
    "post_list_unavailable": "视频号作品列表接口暂时不可用,请稍后重试。",
}


def _play_fail_message(why):
    """把现取失败原因翻成人话 —— 旧文案一律写「视频号未运行」,会误导。"""
    w = str(why or "")
    code = w.split(":", 1)[1] if w.startswith("errCode:") else ""
    if code in ("300333", "300330"):
        return "该账号登录态已被服务端判为过期,请重新扫码登录后再试。"
    if code:
        return f"视频号接口返回 errCode={code},自动取新地址失败,请稍后重试或到视频号后台查看。"
    if w.startswith("http:"):
        return f"该账号登录态不可用({w[5:]}),请重新扫码登录后再试。"
    if w.startswith("request_error:"):
        return f"请求视频号接口失败({w[14:]}),请检查网络后重试。"
    return _PLAY_FAIL_MSG.get(
        w, f"自动取新地址失败({w or 'unknown'}),请稍后重试或到视频号后台查看。")


@app.get("/api/posts/play")
async def post_play(finder_id: str = Query(""), object_id: str = Query("")):
    """取作品播放地址。存的地址过期时用该视频号的 HTTP 登录态现取(rev64)。

    CDN 直链带时效(实测旧地址 400 / 新地址 206)。旧实现必须先有运行中的 worker
    且 worker.context 非空才肯现取;纯 HTTP 模式的账号没有浏览器 context,于是
    明明在跑却被误报「视频号未运行」。现在按 finder_id 从 config 取账号直接
    wx_http 打 post_list,与 worker / 浏览器完全解耦。
    """
    if not finder_id or not object_id:
        raise HTTPException(400, "缺少 finder_id / object_id")
    row = storage.get_post_video(finder_id, object_id)
    if row is None:
        raise HTTPException(404, "作品不存在")
    url = row.get("video_url") or ""
    refreshed = False
    why = ""
    ok_now = await asyncio.to_thread(_url_playable, url)
    if not ok_now:
        acc = _account_for_finder(finder_id)
        if acc is None:
            why = "no_account_for_finder"
        else:
            fresh, why = await PostFetcher(storage, acc, config).find_video_url_http(object_id)
            if fresh and await asyncio.to_thread(_url_playable, fresh):
                storage.update_post_video(finder_id, object_id, fresh)
                url, refreshed, ok_now, why = fresh, True, True, ""
            if not refreshed:
                # 兜底:HTTP 这条路走不通时,若 worker 有浏览器 context 仍走老路径
                w = None
                for cand in (manager.workers or {}).values():
                    ca = getattr(cand, "account", None) or {}
                    if (ca.get("_log_finder_id") or "").strip() == finder_id:
                        w = cand
                        break
                if (w is not None and getattr(w, "logged_in", False)
                        and getattr(w, "context", None) is not None):
                    fresh2 = await PostFetcher(storage, w.account, config).find_video_url(w, object_id)
                    if fresh2 and await asyncio.to_thread(_url_playable, fresh2):
                        storage.update_post_video(finder_id, object_id, fresh2)
                        url, refreshed, ok_now, why = fresh2, True, True, ""
    if not (ok_now or await asyncio.to_thread(_url_playable, url)):
        return {"ok": False, "title": row.get("title") or "",
                "message": _play_fail_message(why)}
    return {"ok": True, "url": url, "refreshed": refreshed,
            "title": row.get("title") or ""}


'''


def replace_slice(s, start, end, new, tag):
    """按起止标记整段替换，避免手写几十行 old_string。"""
    assert s.count(start) == 1, (tag, "起点命中次数", s.count(start))
    i = s.index(start)
    assert s.count(end) >= 1, (tag, "终点不存在")
    j = s.index(end, i)
    return s[:i] + new + s[j:]


def main():
    texts = {}
    for p in (PF, SV):
        with io.open(p, encoding="utf-8") as f:
            texts[p] = f.read()

    for path, old, new, tag in PATCHES:
        s = texts[path]
        n = s.count(old)
        assert n == 1, (tag, "命中次数", n)
        texts[path] = s.replace(old, new, 1)
        print(f"[OK ] {tag}")

    texts[PF] = replace_slice(
        texts[PF], "    async def find_video_url(self, worker, object_id",
        "    # ---------------- 封面 ----------------", NEW_FIND, "pf 重写现取函数")
    print("[OK ] pf 重写现取函数")

    texts[SV] = replace_slice(
        texts[SV], "def _url_playable(url, timeout=8):",
        "@app.websocket(\"/api/remote/ws\")", NEW_PLAY, "server 重写播放接口")
    print("[OK ] server 重写播放接口")

    for path, s in texts.items():
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(s)

    print("\n== 语法 / 自校验 ==")
    for path in (PF, SV):
        s = io.open(path, encoding="utf-8").read()
        ast.parse(s)
        print("  OK  AST", os.path.basename(path))
    pf = io.open(PF, encoding="utf-8").read()
    sv = io.open(SV, encoding="utf-8").read()
    checks = [
        ("pf 新增 HTTP 现取", "async def find_video_url_http" in pf),
        ("pf HTTP 优先", "fresh, why = await self.find_video_url_http(object_id)" in pf),
        ("pf 不再因 context 空直接放弃", "if worker is None or worker.context is None:\n            return \"\"" in pf),
        ("pf 翻页上限可配", "play_max_pages" in pf),
        ("pf total 早停", "pg * ps >= total" in pf),
        ("pf 记录失败原因", "_last_post_list_why" in pf),
        ("server 按 config 找账号", "def _account_for_finder" in sv),
        ("server 调 HTTP 现取", "find_video_url_http(object_id)" in sv),
        ("server 写库前验可播", "_url_playable, fresh" in sv),
        ("server 旧误导文案已移除", "视频号未运行/作品太旧不在列表前几页" not in sv),
        ("server 失败原因映射", "def _play_fail_message" in sv),
        ("server 保留浏览器兜底", "find_video_url(w, object_id)" in sv),
    ]
    bad = 0
    for name, ok in checks:
        print("  %s %s" % ("OK " if ok else "BAD", name))
        if not ok:
            bad += 1
    print("\n[DONE]" if not bad else "\n[FAIL] %d 项" % bad)
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
