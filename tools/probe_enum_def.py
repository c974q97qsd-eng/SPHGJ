# -*- coding: utf-8 -*-
"""搜 visibleType 枚举数值定义(TS 编译特征),纯只读。"""
import asyncio, io, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"


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

        pats = [
            (r'=\d+\]="public"', "ts_enum_rev"),
            (r'\{[^{}]{0,60}public\s*:\s*\d+[^{}]{0,60}self\s*:\s*\d+', "obj_lit"),
            (r'[A-Za-z_$][\w$]{0,30}\.public\s*=\s*\d+', "ts_enum_fwd"),
            (r'VisibleType', "name"),
            (r'self\s*:\s*\d+\s*[,}][^;]{0,40}(公开|private)', "obj_lit2"),
        ]
        found = []
        for u in urls:
            try:
                rr = await page.context.request.get(u, timeout=30000)
                t = await rr.text()
            except Exception:
                continue
            for pat, tag in pats:
                for m in re.finditer(pat, t):
                    a, b = max(0, m.start() - 220), min(len(t), m.end() + 220)
                    found.append({"tag": tag, "u": u[-60:], "ctx": t[a:b]})
        io.open(os.path.join(OUT, "enum_def.json"), "w", encoding="utf-8").write(
            json.dumps(found, ensure_ascii=False, indent=2))
        print("matches:", len(found))
        # 顺带打印最有可能是定义的几条
        for f in found:
            if f["tag"] in ("ts_enum_rev", "obj_lit", "ts_enum_fwd"):
                seg = f["ctx"].replace("\n", " ")
                print(f"[{f['tag']}] {seg[:260]}")
        await c.close()


asyncio.run(main())
