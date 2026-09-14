"""P0 探测:作品列表字段 + 隐藏接口存在性 + 封面可下载性。

安全声明
--------
* **只读**:post_list 只翻 2 页,不写、不改任何数据。
* **隐藏接口只用非法 objectId 探测**(如 "__probe_invalid__"),
  仅判断接口是否存在(**Cannot POST = 不存在**),绝不会命中任何真实作品。
  因此本脚本**不会**把你的任何作品设为隐藏。
* 封面只下载 1 张做连通性测试。

产出:.workbuddy/posts_probe/ 下
  post_list_p1.json / post_list_p2.json  原始返回
  hide_probe.json                         隐藏接口候选探测结果
  cover_probe.txt                         封面下载测试
  summary.txt                             字段清单摘要

用法:
  python tools/probe_posts.py [--profile ./profiles/xxx] [--pages 2]
"""
import argparse
import asyncio
import json
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_URL = "https://channels.weixin.qq.com/micro/interaction/comment"
AUTH_BASE = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin"

OUT = ".workbuddy/posts_probe"

# 只探存在性,body 用非法 objectId —— 不会命中真实作品
HIDE_CANDIDATES = [
    "post/set_visible", "post/set_post_visible", "post/update_visible",
    "post/set_private", "post/update_post_status", "post/set_status",
    "post/modify_post", "post/update_post", "post/set_visibility",
    "post/change_visible", "post/hide_post", "post/unhide_post",
    "post/set_post_private", "post/update_visible_status",
    "post/set_visible_status", "post/op_post", "post/operate_post",
]

# 疑似"作品数据/播放量"接口(仅当 post_list 里没有播放字段时才需要)
STAT_CANDIDATES = [
    "post/get_post_data", "post/post_data", "post/get_data",
    "post/data_overview", "post/get_object_data", "post/post_detail",
    "post/get_post_detail", "post/get_detail",
]


def body_base(finder_id, **extra):
    b = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    b.update(extra)
    return b


async def call(page, base, path, body, aid):
    url = f"{base}/{path}?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
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
    return await page.evaluate(js, {"url": url, "body": body})


def cls(resp_text):
    """按返回文本分类:MISS(接口不存在) / 其它原样返回。"""
    if "Cannot POST" in resp_text or "Cannot GET" in resp_text:
        return "MISS"
    return "EXIST"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<探测profile>")
    ap.add_argument("--pages", type=int, default=2, help="翻几页 post_list(尽量小)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--page-size", dest="page_size", type=int, default=20)
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    log = []

    def p(s=""):
        print(s)
        log.append(str(s))

    async with async_playwright() as pw:
        p(f"profile = {args.profile}")
        ctx = await launch_stealth(pw, args.profile, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # post_list 的 _pageUrl 是评论页,必须在同源页面里 fetch 才带 cookie
        await page.goto(PAGE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        info = await page.evaluate("""() => {
            const aid = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
            const fid = localStorage.getItem('finder_username') || '';
            let a = aid;
            try { a = JSON.parse(aid); } catch(e) { a = aid.replace(/^"|"$/g, ''); }
            return { aid: a, fid: fid, url: location.href };
        }""")
        aid, fid = info["aid"], info["fid"]
        p(f"URL: {info['url']}")
        p(f"AID: {aid[:40]}")
        p(f"FID: {fid[:40]}")
        if not aid or not fid:
            p("!! 未取到 _aid / _log_finder_id —— 该 profile 可能未登录,请换一个已登录账号")
            await ctx.close()
            return

        # ---------- 1. post_list ----------
        last_buff = ""
        all_posts = []
        for i in range(1, args.pages + 1):
            raw = await call(
                page, BASE, "post/post_list",
                body_base(fid, pageSize=args.page_size, currentPage=i,
                          userpageType=11, stickyOrder=False),
                aid,
            )
            with open(f"{OUT}/post_list_p{i}.json", "w", encoding="utf-8") as f:
                f.write(raw)
            try:
                j = json.loads(raw)
            except Exception:
                p(f"p{i} 非 JSON: {raw[:200]}")
                break
            data = j.get("data") or {}
            lst = data.get("list") or []
            p(f"p{i}: errCode={j.get('errCode')} 条数={len(lst)} "
              f"totalCount={data.get('totalCount')} continueFlag={data.get('continueFlag')}")
            all_posts.extend(lst)
            total = data.get("totalCount") or 0
            if not lst or len(lst) < args.page_size or len(all_posts) >= total:
                break
            await asyncio.sleep(1.5)

        if not all_posts:
            p("!! post_list 没拿到作品,后面跳过")
            await ctx.close()
            return

        # ---------- 2. 字段清单 ----------
        keys = {}
        for v in all_posts:
            for k, val in v.items():
                keys.setdefault(k, [])
                if len(keys[k]) < 3:
                    keys[k].append(val)
        p("\n=== 作品字段清单(共 %d 个字段) ===" % len(keys))
        for k in sorted(keys):
            sample = keys[k][0]
            s = str(sample)
            if len(s) > 70:
                s = s[:70] + "..."
            p(f"  {k:34} = {s}")

        # 重点字段猜测
        p("\n=== 关键字段定位 ===")
        lower = {k.lower(): k for k in keys}
        want = {
            "标题": ["title", "description", "desc", "objectdesc", "content"],
            "封面": ["coverurl", "cover", "thumburl", "thumb", "imageurl", "picurl",
                    "objectcover", "coverimg", "poster"],
            "播放": ["playcount", "play", "viewcount", "readcount", "pv", "plays"],
            "点赞": ["likecount", "like", "praisecount", "likes"],
            "评论": ["commentcount", "comment"],
            "转发": ["sharecount", "share", "forwardcount"],
            "收藏": ["collectcount", "collect", "favcount", "favoritecount"],
            "时间": ["createtime", "createts", "publish", "time", "posttime"],
            "可见": ["visible", "status", "privacy", "private", "isshow", "visiblestatus"],
            "更新": ["updatetime", "modifytime", "lastmodify", "edittime"],
        }
        for why, cands in want.items():
            hit = [lower[c] for c in cands if c in lower]
            p(f"  {why:4} -> {hit if hit else '(未找到)'}")

        # ---------- 3. 封面下载测试 ----------
        p("\n=== 封面下载测试 ===")
        cover_url = ""
        _m = (((all_posts[0].get("desc") or {}).get("media") or [{}])[0]) or {}
        for c in ("coverUrl", "thumbUrl", "fullCoverUrl", "shareCoverUrl"):
            if _m.get(c):
                cover_url = _m[c]
                break
        if not cover_url:
            # 兜底:递归找 url 里带 http 且像图片的
            def find_img(o, depth=0):
                if depth > 3:
                    return ""
                if isinstance(o, dict):
                    for k, v in o.items():
                        if isinstance(v, str) and v.startswith("http") and \
                           any(t in v.lower() for t in (".jpg", ".jpeg", ".png", ".webp", "mmecoa", "wx_fmt")):
                            return v
                    for v in o.values():
                        r = find_img(v, depth + 1)
                        if r:
                            return r
                elif isinstance(o, list):
                    for v in o:
                        r = find_img(v, depth + 1)
                        if r:
                            return r
                return ""
            cover_url = find_img(all_posts[0])
        p(f"  封面 URL: {str(cover_url)[:120]}")
        cover_res = {"url": cover_url, "ok": False}
        if cover_url:
            try:
                r = await ctx.request.get(
                    cover_url,
                    headers={"Referer": "https://channels.weixin.qq.com/"},
                    timeout=15000,
                )
                body_bytes = await r.body()
                cover_res = {
                    "url": cover_url, "status": r.status,
                    "content_type": r.headers.get("content-type", ""),
                    "bytes": len(body_bytes), "ok": r.status == 200,
                }
                p(f"  context.request.get -> status={r.status} "
                  f"type={r.headers.get('content-type','')} bytes={len(body_bytes)}")
                if r.status == 200 and len(body_bytes) > 1024:
                    with open(f"{OUT}/cover_sample.bin", "wb") as f:
                        f.write(body_bytes)
                    p("  已保存 cover_sample.bin(可下载,方案里走这条路)")
                else:
                    p("  !! 下载失败或过小 —— 需要改用页面内 fetch 转 base64")
            except Exception as e:
                cover_res = {"url": cover_url, "err": str(e), "ok": False}
                p(f"  下载异常: {e}")
        with open(f"{OUT}/cover_probe.txt", "w", encoding="utf-8") as f:
            f.write(json.dumps(cover_res, ensure_ascii=False, indent=2))

        # ---------- 4. 隐藏接口存在性(非法 objectId,零风险) ----------
        p("\n=== 隐藏/可见性接口探测(非法 objectId,不会命中真实作品) ===")
        hide_res = {}
        for path in HIDE_CANDIDATES:
            raw = await call(
                page, BASE, path,
                body_base(fid, objectId="__probe_invalid__",
                          exportId="__probe_invalid__",
                          visibleType=2, visible=2, status=2,
                          opType=1, isPrivate=1),
                aid,
            )
            tag = cls(raw)
            hide_res[path] = {"tag": tag, "resp": raw[:300]}
            p(f"  {tag:5} {path:32} {raw[:90]}")
            await asyncio.sleep(1.2)
        with open(f"{OUT}/hide_probe.json", "w", encoding="utf-8") as f:
            json.dump(hide_res, f, ensure_ascii=False, indent=2)

        hit = [k for k, v in hide_res.items() if v["tag"] == "EXIST"]
        p(f"\n存在的候选 {len(hit)} 个: {hit}")

        # ---------- 5. 数据/播放量接口(仅当列表里没有播放字段时) ----------
        has_play = any(c in lower for c in
                       ("playcount", "play", "viewcount", "readcount", "pv"))
        p(f"\n=== 播放量字段 {'已在 post_list 内' if has_play else '不在列表内,探测专用接口'} ===")
        stat_res = {}
        if not has_play:
            oid = all_posts[0].get("objectId") or all_posts[0].get("exportId") or ""
            for path in STAT_CANDIDATES:
                raw = await call(page, BASE, path,
                                 body_base(fid, objectId=oid, exportId=oid), aid)
                tag = cls(raw)
                stat_res[path] = {"tag": tag, "resp": raw[:300]}
                p(f"  {tag:5} {path:32} {raw[:90]}")
                await asyncio.sleep(1.2)
            with open(f"{OUT}/stat_probe.json", "w", encoding="utf-8") as f:
                json.dump(stat_res, f, ensure_ascii=False, indent=2)

        p("\n保持浏览器 15s 便于观察...")
        await asyncio.sleep(15)
        await ctx.close()

    with open(f"{OUT}/summary.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(log))
    print(f"\n已写入 {OUT}/summary.txt")


asyncio.run(main())
