# -*- coding: utf-8 -*-
"""实验:headless Chromium 下 CDP Page.startScreencast 能否稳定出帧 + 输入注入生效。

不碰母本,用本地临时 profile 跑。只验证技术可行性。
"""
import asyncio
import base64
import os
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")
from playwright.async_api import async_playwright  # noqa: E402

PROFILE = os.path.join(os.environ.get("TEMP", "."), "_rv_probe_profile")


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=True,
            viewport={"width": 1280, "height": 800},
            args=["--disable-gpu", "--disable-software-rasterizer"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        cdp = await ctx.new_cdp_session(page)

        frames = []
        t0 = time.time()

        async def on_frame(ev):
            frames.append((time.time() - t0, len(ev.get("data") or ""),
                           ev.get("metadata", {}).get("deviceWidth")))
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 60,
            "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 1})

        # 页面有变化才会出新帧:制造几次变化
        for i in range(5):
            await page.evaluate(f"document.body.style.background = 'hsl({i*70},70%,80%)'")
            await asyncio.sleep(0.4)
        await page.wait_for_timeout(800)

        print(f"帧数 {len(frames)}")
        for ts, ln, w in frames[:6]:
            print(f"  t={ts:.2f}s size={ln}B b64 deviceWidth={w}")

        if frames:
            # 存第一帧看看是不是真图
            raw = frames and None
            data = None
            async def grab():
                nonlocal data
                ev = await asyncio.wait_for(
                    _wait_frame(cdp), timeout=5)
                data = ev
            # 简单点:再触发一次变化,拿 base64 存文件
            got = {}

            def on_f2(ev):
                got["d"] = ev.get("data")

            cdp2 = cdp
            cdp2.on("Page.screencastFrame", on_f2)
            await page.evaluate("document.title='probe'")
            await asyncio.sleep(1.0)
            if got.get("d"):
                out = os.path.join(os.environ.get("TEMP", "."), "_rv_frame.jpg")
                open(out, "wb").write(base64.b64decode(got["d"]))
                print("帧已存:", out, os.path.getsize(out), "B")

            # 输入注入验证:点 example.com 的链接
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": 100, "y": 200,
                "button": "left", "clickCount": 1})
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": 100, "y": 200,
                "button": "left", "clickCount": 1})
            await asyncio.sleep(0.6)
            await cdp.send("Input.insertText", {"text": "hello"})
            print("输入注入已发送(未报错即通道可用)")
            print("当前页面 url:", page.url)

        await cdp.send("Page.stopScreencast")
        await ctx.close()


async def _wait_frame(cdp):
    fut = asyncio.get_event_loop().create_future()

    def h(ev):
        if not fut.done():
            fut.set_result(ev)

    cdp.on("Page.screencastFrame", h)
    return await fut


asyncio.run(main())
