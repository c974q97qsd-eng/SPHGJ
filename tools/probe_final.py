# -*- coding: utf-8 -*-
"""P0 收尾:一次性跑完所有待确认项(避免反复启停浏览器导致登录态失效)。

会做的事(**一次启动全部做完**):
  1. 取 _aid / _log_finder_id
  2. 测 pageSize 上限: 20 / 50 / 100 各拉 1 页,看返回条数与 totalCount
  3. 抓 1 页真实作品落盘(供 P1 建表与前端 mock)
  4. 封面下载验证(context.request.get,带 cookie)
  5. visibleType 枚举: 对**指定作品**切到候选值,验证生效后立即切回原值

⚠️ 第 5 步会短暂改动一个真实作品的可见性(约 3 秒后还原)。
   --target N 指定用列表第 N 条(默认 -1 = 当页最后一条,即最老的、影响最小的)。
   --dry-run 则跳过第 5 步,只做 1-4。

用法:
  python tools/probe_final.py --profile ./profiles/xxx            # 含真实切换(自动还原)
  python tools/probe_final.py --profile ./profiles/xxx --dry-run  # 只读,零风险
"""
import argparse
import asyncio
import io
import json
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"
COMMENT_URL = "https://channels.weixin.qq.com/micro/interaction/comment"
BASE = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin"

FETCH = r"""async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify(body)
  });
  const t = await r.text();
  return {status: r.status, body: t.slice(0, 2000000)};
}"""

GET_IDS = r"""() => {
  const raw = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
  let a = raw;
  try { a = JSON.parse(raw); } catch (e) { a = raw.replace(/^"|"$/g, ''); }
  return {aid: a, fid: localStorage.getItem('finder_username') || '', href: location.href};
}"""


def base_body(fid, **extra):
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


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--wait", type=int, default=15000)
    ap.add_argument("--target", type=int, default=-1, help="用第几条作品做切换测试,默认最后一条")
    ap.add_argument("--dry-run", action="store_true", help="跳过真实切换,零风险")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    rep = {"profile": args.profile, "steps": {}}

    async with async_playwright() as pw:
        c = await launch_stealth(pw, args.profile)
        page = c.pages[0] if c.pages else await c.new_page()

        # 1) 取 aid/fid(评论页才有 finder_username)
        try:
            await page.goto(COMMENT_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto comment:", e)
        await page.wait_for_timeout(7000)
        ids = await page.evaluate(GET_IDS)
        aid, fid = ids["aid"], ids["fid"]
        rep["steps"]["ids"] = {"aid": (aid or "")[:12] + "...", "fid": (fid or "")[:20] + "..."}
        print(f"aid={str(aid)[:14]}  fid={str(fid)[:26]}")
        if not aid or not fid:
            print("!! 未登录 —— 请先在 SPHGJ 里登录该账号")
            rep["error"] = "not logged in"
            io.open(os.path.join(OUT, "final.json"), "w", encoding="utf-8").write(
                json.dumps(rep, ensure_ascii=False, indent=2))
            await c.close()
            return

        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto post/list:", e)
        await page.wait_for_timeout(args.wait)

        def url_for(path):
            return f"{BASE}/{path}?_aid={aid}&_pageUrl={quote(PAGE_URL)}"

        # 2) pageSize 上限
        sizes = {}
        for ps in (20, 50, 100, 200):
            r = await page.evaluate(FETCH, {
                "url": url_for("post/post_list"),
                "body": base_body(fid, pageSize=ps, currentPage=1,
                                  userpageType=11, stickyOrder=True)})
            try:
                j = json.loads(r["body"])
                got = len(((j.get("data") or {}).get("list")) or [])
                sizes[ps] = {"errCode": j.get("errCode"), "got": got,
                             "total": (j.get("data") or {}).get("totalCount")}
                print(f"  pageSize={ps:4} -> errCode={j.get('errCode')} 返回 {got} 条")
            except Exception as e:
                sizes[ps] = {"errCode": "parse_fail", "got": 0, "raw": r["body"][:150]}
                print(f"  pageSize={ps:4} -> 解析失败 {r['body'][:100]}")
            await page.wait_for_timeout(1800)
        rep["steps"]["page_size"] = sizes

        # 3) 抓 1 页真实样本落盘
        r = await page.evaluate(FETCH, {
            "url": url_for("post/post_list"),
            "body": base_body(fid, pageSize=20, currentPage=1,
                              userpageType=11, stickyOrder=True)})
        try:
            j = json.loads(r["body"])
            lst = ((j.get("data") or {}).get("list")) or []
            io.open(os.path.join(OUT, "sample_page.json"), "w", encoding="utf-8").write(
                json.dumps(j, ensure_ascii=False, indent=2))
            print(f"  样本落盘: {len(lst)} 条 -> sample_page.json")
            rep["steps"]["sample"] = {"count": len(lst)}
        except Exception as e:
            lst = []
            print("  样本解析失败:", e)

        # 4) 封面下载验证
        cover = None
        if lst:
            m0 = ((lst[0].get("desc") or {}).get("media") or [{}])[0]
            cu = m0.get("coverUrl") or m0.get("thumbUrl")
            if cu:
                try:
                    resp = await page.context.request.get(
                        cu, headers={"Referer": PAGE_URL}, timeout=20000)
                    b = await resp.body()
                    cover = {"status": resp.status,
                             "ct": (resp.headers or {}).get("content-type"),
                             "bytes": len(b)}
                    print(f"  封面: {resp.status} {cover['ct']} {len(b)}B")
                    if resp.status == 200:
                        io.open(os.path.join(OUT, "cover_sample.jpg"), "wb").write(b)
                except Exception as e:
                    cover = {"error": str(e)[:150]}
                    print("  封面下载失败:", e)
        rep["steps"]["cover"] = cover

        # 4.5) Python 侧下载官方 JS,搜「仅自己可见」的枚举映射(零风险)
        KWS = ["\u4ec5\u81ea\u5df1\u53ef\u89c1", "updateVisible",
               "post_update_visible", "visibleType"]
        try:
            urls = await page.evaluate(
                "() => performance.getEntriesByType('resource')"
                ".map(e => e.name).filter(u => /\\.(js|mjs)(\\?|$)/.test(u))")
            hits, fails = [], 0
            for u in urls:
                try:
                    rr = await page.context.request.get(u, timeout=30000)
                    t = await rr.text()
                except Exception:
                    fails += 1
                    continue
                for kw in KWS:
                    i = t.find(kw)
                    n = 0
                    while i >= 0 and n < 3:
                        hits.append({"u": u[-70:], "kw": kw,
                                     "ctx": t[max(0, i - 320):i + 320]})
                        n += 1
                        i = t.find(kw, i + 1)
            io.open(os.path.join(OUT, "js_enum.json"), "w", encoding="utf-8").write(
                json.dumps(hits, ensure_ascii=False, indent=2))
            print(f"  JS enum: urls={len(urls)} fetch_fail={fails} hits={len(hits)}"
                  f" -> js_enum.json")
        except Exception as e:
            print("  JS enum FAIL:", e)

        # 5) visibleType 枚举(真实切换 + 立即还原)
        if args.dry_run or not lst:
            rep["steps"]["visible"] = "skipped (dry-run)"
            print("  跳过真实切换(--dry-run)")
        else:
            post = lst[args.target]
            oid = post.get("exportId") or post.get("objectId")
            cur = post.get("visibleType")
            title = (post.get("desc") or {}).get("description", "")[:30]
            print(f"  测试作品: visibleType={cur}  {title}")
            found = None
            for cand in (2, 0, 3, 4, 5):
                if cand == cur:
                    continue
                r = await page.evaluate(FETCH, {
                    "url": url_for("post/post_update_visible"),
                    "body": base_body(fid, objectId=oid, visibleType=cand)})
                ok = '"errCode":0' in r["body"].replace(" ", "")
                print(f"    -> 切到 {cand}: {'OK' if ok else r['body'][:90]}")
                rep.setdefault("steps", {}).setdefault("visible_try", []).append(
                    {"cand": cand, "ok": ok, "resp": r["body"][:200]})
                if ok:
                    # 复核:重新拉列表看值是否真的变了
                    await page.wait_for_timeout(1500)
                    r2 = await page.evaluate(FETCH, {
                        "url": url_for("post/post_list"),
                        "body": base_body(fid, pageSize=20, currentPage=1,
                                          userpageType=11, stickyOrder=True)})
                    try:
                        lst2 = ((json.loads(r2["body"]).get("data") or {}).get("list")) or []
                        now = next((x.get("visibleType") for x in lst2
                                    if (x.get("exportId") or x.get("objectId")) == oid), None)
                    except Exception:
                        now = None
                    print(f"       复核 visibleType: {cur} -> {now}")
                    found = {"value": cand, "verified": now == cand, "now": now}
                    break
                await page.wait_for_timeout(1500)

            # 还原
            if found:
                r = await page.evaluate(FETCH, {
                    "url": url_for("post/post_update_visible"),
                    "body": base_body(fid, objectId=oid, visibleType=cur)})
                ok = '"errCode":0' in r["body"].replace(" ", "")
                print(f"    -> 还原到 {cur}: {'OK' if ok else r['body'][:90]}")
                found["restored"] = ok
                rep["steps"]["visible"] = {"current": cur, "hidden": found}
            else:
                rep["steps"]["visible"] = {"current": cur, "hidden": "未找到可用枚举值"}

        await c.close()

    io.open(os.path.join(OUT, "final.json"), "w", encoding="utf-8").write(
        json.dumps(rep, ensure_ascii=False, indent=2))
    print("-> final.json")


if __name__ == "__main__":
    asyncio.run(main())
