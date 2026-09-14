"""从「内容管理-视频」页加载的官方 JS bundle 里扒出所有接口路径。

为什么这么做:
  猜接口名这条路已经走死(46 个候选全 Cannot POST)。但官方前端的 JS 里
  **一定硬编码了全部接口路径**,直接正则抽出来即可,零风险、零额外请求。

产出: .workbuddy/posts_probe/js_apis.txt
  - 所有形如 "post/xxx" / "finder/xxx" 的接口路径 + 出现次数 + 上下文片段

用法: python tools/scan_js_apis.py --profile ./profiles/xxx
"""
import argparse
import asyncio
import io
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
URL = "https://channels.weixin.qq.com/platform/post/list"

# 接口路径通常写成 "post/post_list" 或 '/post/post_list'
PAT = re.compile(r"""['"`](/?)(\w+(?:/\w+){1,3})['"`]""")
PREFIX = ("post", "finder", "object", "feed", "visible", "comment", "collection",
          "live", "statistic", "mod", "op")
# 隐藏相关关键词
HIDE_WORDS = ("visible", "hide", "hidden", "private", "privacy", "show", "display",
              "仅自己", "公开", "下架", "off", "shelf")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--wait", type=int, default=15000)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    blobs = []      # (url, js_text)
    counter = Counter()
    ctx_hits = {}   # path -> 上下文片段

    async with async_playwright() as pw:
        c = await launch_stealth(pw, args.profile, headless=args.headless)
        page = c.pages[0] if c.pages else await c.new_page()

        async def on_resp(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "javascript" not in ct and not resp.url.endswith(".js"):
                    return
                if resp.status != 200:
                    return
                t = await resp.text()
                if len(t) > 400_000:      # 超大 bundle 跳过,省内存
                    return
                blobs.append((resp.url, t))
            except Exception:
                pass

        page.on("response", on_resp)
        print(f"goto {URL} ...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("  goto:", e)
        await page.wait_for_timeout(args.wait)

        # 同时把页面里的 iframe 也扫一遍 DOM(找隐藏按钮)
        dom = []
        for fr in page.frames:
            try:
                r = await fr.evaluate(r"""() => {
                    const out = [];
                    document.querySelectorAll('*').forEach(el => {
                        if (el.children.length) return;
                        const t = (el.textContent || '').trim();
                        if (!t || t.length > 20) return;
                        if (/隐藏|仅自己|可见|公开|下架|删除|更多/.test(t)) {
                            out.push({
                                tag: el.tagName, txt: t,
                                cls: (el.className || '').toString().slice(0, 100),
                                frame: location.pathname,
                                rect: (() => { const r = el.getBoundingClientRect();
                                    return Math.round(r.x) + ',' + Math.round(r.y) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height); })()
                            });
                        }
                    });
                    return out.slice(0, 200);
                }""")
                dom.extend(r or [])
            except Exception:
                pass

        await c.close()

    print(f"抓到 {len(blobs)} 个 JS")
    for url, t in blobs:
        for m in PAT.finditer(t):
            p = m.group(2)
            top = p.split("/")[0]
            if top not in PREFIX:
                continue
            counter[p] += 1
            if p not in ctx_hits:
                s = max(0, m.start() - 90)
                ctx_hits[p] = t[s:m.end() + 90].replace("\n", " ")

    lines = ["=== 接口路径清单(按前缀过滤) ==="]
    for p, n in counter.most_common():
        kw = "  <<< 隐藏相关" if any(w in p.lower() for w in HIDE_WORDS) else ""
        lines.append(f"{p:48} x{n}{kw}")
    lines.append("")
    lines.append("=== 隐藏相关接口的上下文 ===")
    for p, n in counter.most_common():
        if any(w in p.lower() for w in HIDE_WORDS):
            lines.append(f"\n--- {p} (x{n}) ---\n{ctx_hits.get(p, '')}")
    lines.append("")
    lines.append("=== DOM: 操作类元素 ===")
    seen = set()
    for o in dom:
        k = o["txt"] + "|" + o["cls"]
        if k in seen:
            continue
        seen.add(k)
        lines.append(f"[{o['tag']}] {o['txt']:12} cls={o['cls']}  rect={o['rect']}")

    io.open(os.path.join(OUT, "js_apis.txt"), "w", encoding="utf-8").write("\n".join(lines))
    print("-> js_apis.txt  接口数:", len(counter), " DOM 命中:", len(dom))


if __name__ == "__main__":
    asyncio.run(main())
