"""Playwright Chromium 启动:统一反检测(stealth)配置。

视频号助手登录页/平台页会检测 headless / 自动化环境(navigator.webdriver、
HeadlessChrome UA 等),检测到就不渲染二维码或拦截接口。此处统一加
--disable-blink-features=AutomationControlled、隐藏 webdriver、设正常 UA,
让 headless Chromium 尽量接近真实浏览器。仍抓不到时用「在新窗口扫码」(headed)兜底。
"""
import logging
logger = logging.getLogger("sphgj")

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]

# 在每个页面加载前注入,抹掉常见自动化指纹
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

# 正常 Chrome UA(去掉 HeadlessChrome 标志)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


async def launch_stealth(playwright, profile_dir, headless=True):
    """启动持久化 Chromium context,带反检测配置。

    登录会话与账号 worker 共用,保证视频号页面看到的环境一致。
    """
    ctx = await playwright.chromium.launch_persistent_context(
        profile_dir, headless=headless,
        viewport={"width": 1280, "height": 800},
        args=STEALTH_ARGS, user_agent=DEFAULT_UA)
    await ctx.add_init_script(STEALTH_INIT_SCRIPT)
    logger.debug(f"[browser] launch_stealth headless={headless} profile={profile_dir}")
    return ctx
