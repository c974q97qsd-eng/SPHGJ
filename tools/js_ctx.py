"""在页面 JS 里搜索指定关键词的上下文(用来反推接口参数结构)。

用法: python tools/js_ctx.py "kw1,kw2" [--width 800]
产出: .workbuddy/posts_probe/js_ctx.txt
"""
import argparse
import asyncio
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
URL = "https://channels.weixin.qq.com/platform/post/list"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kws")
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--wait", type=int, default=15000)
    ap.add_argument("--width", type=int, default=700)
    ap.add_argument("--max", type=int, default=6, help="每个关键词最多保留几处")
    args = ap.parse_args()

    kws = [k.strip() for k in args.kws.split(",") if k.strip()]
    os.makedirs(OUT, exist_ok=True)
    blobs = []

    async with async_playwright() as pw:
        c = await launch_stealth(pw, args.profile)
        page = c.pages[0] if c.pages else await c.new_page()

        async def on_resp(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "javascript" not in ct and not resp.url.endswith(".js"):
                    return
                if resp.status != 200:
                    return
                t = await resp.text()
                if len(t) > 600_000:
                    return
                blobs.append((resp.url, t))
            except Exception:
                pass

        page.on("response", on_resp)
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(args.wait)
        await c.close()

    lines = [f"JS 文件数: {len(blobs)}"]
    for kw in kws:
        lines.append("\n" + "=" * 70 + f"\n关键词: {kw}\n" + "=" * 70)
        n = 0
        for url, t in blobs:
            for m in re.finditer(re.escape(kw), t):
                if n >= args.max:
                    break
                s = max(0, m.start() - args.width)
                e = min(len(t), m.end() + args.width)
                lines.append(f"\n--- [{os.path.basename(url)[:60]}] pos={m.start()} ---")
                lines.append(t[s:e].replace("\n", " "))
                n += 1
            if n >= args.max:
                break
        if n == 0:
            lines.append("(未命中)")

    io.open(os.path.join(OUT, "js_ctx.txt"), "w", encoding="utf-8").write("\n".join(lines))
    print("-> js_ctx.txt")


if __name__ == "__main__":
    asyncio.run(main())
