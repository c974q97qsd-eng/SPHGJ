"""探测视频号「扫码后选择账号」页面结构 + 账号列表 API。

用已登录 profile 启动,打开 platform,尝试:
1. dump 顶栏账号区域的 DOM
2. 找账号切换入口并点击,dump 弹层
3. 监听 XHR,找账号列表类接口
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "./profiles/<账号profile>"
MAIN = "https://channels.weixin.qq.com/platform"
OUT = ".workbuddy/account_switch"


async def main():
    os.makedirs(OUT, exist_ok=True)
    net = []

    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, PROFILE, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        def on_req(req):
            u = req.url
            if "cgi-bin" in u or "finder" in u:
                net.append(("REQ", req.method, u[:200]))

        page.on("request", on_req)

        await page.goto(MAIN, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        print("URL:", page.url)
        print("TITLE:", await page.title())

        # 判断是否已登录
        logged = await page.evaluate("""() => {
            const aid = localStorage.getItem('__ml::aid') || '';
            const fid = localStorage.getItem('finder_username') || '';
            return { aid: aid.slice(0,40), fid: fid.slice(0,40) };
        }""")
        print("LOCALSTORAGE:", logged)

        await page.screenshot(path=f"{OUT}/01_main.png", full_page=False)

        # 1. dump 顶栏账号区域候选
        cands = [
            ".account-info", ".account-info .name", ".finder-info",
            ".user-info", ".header-account", ".account-switch",
            "[class*='account']", "[class*='Account']",
            ".avatar", ".nickname", ".name",
        ]
        print("\n=== DOM 候选 ===")
        for sel in cands:
            try:
                loc = page.locator(sel)
                n = await loc.count()
                if n == 0:
                    continue
                for i in range(min(n, 3)):
                    try:
                        txt = (await loc.nth(i).inner_text(timeout=1500) or "").strip().replace("\n", " | ")
                        cls = await loc.nth(i).get_attribute("class") or ""
                        if txt:
                            print(f"  {sel}[{i}] cls={cls[:60]!r} txt={txt[:80]!r}")
                    except Exception:
                        continue
            except Exception:
                continue

        # 2. 监听后续 XHR(找账号列表接口)
        print("\n=== 监听 XHR 5s(模拟翻动页面) ===")
        net.clear()
        await page.mouse.wheel(0, 200)
        await page.wait_for_timeout(1500)
        await page.wait_for_timeout(4000)
        seen = set()
        for kind, m, u in net:
            if u in seen:
                continue
            seen.add(u)
            print(f"  {m} {u}")

        # 3. 尝试直接调已知账号列表 API
        print("\n=== 尝试账号列表 API ===")
        aid = logged.get("aid", "")
        apis = [
            ("auth_data", "auth/auth_data"),
            ("get_finder_account", "auth/get_finder_account"),
            ("account_list", "account/account_list"),
            ("get_account_list", "auth/get_account_list"),
            ("finder_account_list", "auth/finder_account_list"),
            ("get_finder_account_list", "auth/get_finder_account_list"),
            ("switch_account", "auth/switch_finder_account"),
        ]
        for tag, path in apis:
            try:
                r = await page.evaluate("""async ([path, aid]) => {
                    const base = 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/';
                    const url = base + path + '?_aid=' + aid +
                        '&_pageUrl=' + encodeURIComponent('https://channels.weixin.qq.com/platform');
                    try {
                        const resp = await fetch(url, {method:'POST',
                            headers:{'Content-Type':'application/json'},
                            body:'{}', credentials:'include'});
                        return await resp.text();
                    } catch(e) { return 'ERR:' + e.message; }
                }""", [path, aid])
                preview = r[:400].replace("\n", " ")
                print(f"  [{tag}] {preview}")
            except Exception as e:
                print(f"  [{tag}] EXC {e}")

        # 4. 尝试访问已知的账号选择/切换页
        print("\n=== 尝试账号选择页 URL ===")
        urls = [
            "https://channels.weixin.qq.com/platform/account_switch",
            "https://channels.weixin.qq.com/platform/select_account",
            "https://channels.weixin.qq.com/platform/account_choose",
            "https://channels.weixin.qq.com/platform/login",
        ]
        for u in urls:
            try:
                await page.goto(u, wait_until="domcontentloaded", timeout=8000)
                await page.wait_for_timeout(3000)
                title = await page.title()
                body = (await page.evaluate("() => document.body.innerText") or "").strip().replace("\n", " | ")
                print(f"  {u}")
                print(f"     -> {page.url}")
                print(f"     title={title!r} body={body[:200]!r}")
                await page.screenshot(path=f"{OUT}/url_{u.split('/')[-1]}.png")
            except Exception as e:
                print(f"  {u} EXC {e}")

        await ctx.close()


asyncio.run(main())
