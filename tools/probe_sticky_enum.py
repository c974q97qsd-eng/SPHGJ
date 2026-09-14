# -*- coding: utf-8 -*-
"""搜 StickyOpStatus 枚举数值定义(纯只读:只下载 JS 静态资源,零 API 调用)。"""
import asyncio, io, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"

PATS = [
    r'UnSticky',
    r'NoOperation',
    r'StickyOpStatus',
    r'stickyOpStatus\s*[:=]',
]


async def main():
    profile = sys.argv[1] if len(sys.argv) > 1 else "./profiles/<账号profile>"
    async with async_playwright() as pw:
        c = await launch_stealth(pw, profile)
        page = c.pages[0] if c.pages else await c.new_page()
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto:", e)
        await page.wait_for_timeout(14000)
        urls = await page.evaluate(
            "() => performance.getEntriesByType('resource')"
            ".map(e => e.name).filter(u => /\\.(js|mjs)(\\?|$)/.test(u))")
        print("js urls:", len(urls))
        found = []
        for u in urls:
            try:
                rr = await page.context.request.get(u, timeout=30000)
                t = await rr.text()
            except Exception:
                continue
            for pat in PATS:
                for m in re.finditer(pat, t):
                    a, b = max(0, m.start() - 260), min(len(t), m.end() + 260)
                    found.append({"kw": pat, "u": u[-70:], "ctx": t[a:b]})
        io.open(os.path.join(OUT, "sticky_enum.json"), "w", encoding="utf-8").write(
            json.dumps(found, ensure_ascii=False, indent=2))
        print("matches:", len(found))
        await c.close()


asyncio.run(main())
