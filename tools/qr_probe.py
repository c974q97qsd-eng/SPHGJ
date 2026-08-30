"""一次性探测:headless 下视频号登录页二维码能否截到,以及落在哪个 frame/选择器。

用法: python tools/qr_probe.py [--headed]
输出: 主文档与各 iframe frame 的选择器命中情况 + 截图字节数,截图落 .workbuddy/qr_probe_*.png
"""
import sys
import os
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".workbuddy")
os.makedirs(OUT_DIR, exist_ok=True)

from playwright.async_api import async_playwright  # noqa: E402
from backend.browser import launch_stealth, close_context_safely  # noqa: E402
from backend.selectors import QR_CANDIDATES, LOGIN_URL  # noqa: E402

EXTRA = [
    "img.js_qrcode_img",
    "img.web_qrcode_img",
    ".qrcode img",
    ".qrcode",
    "canvas",
    "img",
]


async def probe(headless: bool):
    profile = tempfile.mkdtemp(prefix="_qrprobe_")
    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, profile, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception as e:
            print("goto err:", e)
        await page.wait_for_timeout(8000)
        print(f"headless={headless} url={page.url}")
        print(f"title={await page.title()}")
        frames = page.frames
        print(f"frames={len(frames)}")
        for i, fr in enumerate(frames):
            print(f"  [{i}] {fr.url[:110]}")
        for i, fr in enumerate(frames):
            tag = "main" if fr == page.main_frame else f"frame{i}"
            for sel in list(QR_CANDIDATES) + EXTRA:
                try:
                    loc = fr.locator(sel).first
                    cnt = await loc.count()
                    if not cnt:
                        continue
                    box = await loc.bounding_box()
                    png = await loc.screenshot(timeout=4000)
                    print(f"  HIT {tag} sel={sel!r} size={box} bytes={len(png)}")
                    p = os.path.join(OUT_DIR, f"qr_probe_{tag}_{abs(hash(sel)) % 10000}.png")
                    with open(p, "wb") as f:
                        f.write(png)
                except Exception as e:
                    print(f"  miss {tag} sel={sel!r}: {type(e).__name__} {str(e)[:60]}")
        # 整页兜底
        try:
            png = await page.screenshot(timeout=6000)
            p = os.path.join(OUT_DIR, f"qr_probe_{'headless' if headless else 'headed'}_full.png")
            with open(p, "wb") as f:
                f.write(png)
            print(f"  full page screenshot bytes={len(png)} -> {p}")
        except Exception as e:
            print("  full screenshot fail:", e)
        await close_context_safely(ctx, profile, "[qr_probe]")
    print("done")


if __name__ == "__main__":
    asyncio.run(probe("--headed" not in sys.argv))
