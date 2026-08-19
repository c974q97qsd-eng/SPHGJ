"""Playwright Chromium 启动:统一反检测(stealth)配置。

视频号助手登录页/平台页会检测 headless / 自动化环境(navigator.webdriver、
HeadlessChrome UA 等),检测到就不渲染二维码或拦截接口。此处统一加
--disable-blink-features=AutomationControlled、隐藏 webdriver、设正常 UA,
让 headless Chromium 尽量接近真实浏览器。仍抓不到时用「在新窗口扫码」(headed)兜底。
"""
import os
import logging
logger = logging.getLogger("sphgj")

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    # B: 内存优化 flag(均不改变 navigator.webdriver/UA/plugins,不影响 stealth 反检测)
    "--disable-extensions",             # 禁扩展,省扩展进程内存
    "--disable-components",             # 禁组件加载
    "--disable-default-apps",           # 禁默认应用
    "--disable-background-networking",  # 禁后台网络(更新检查/Safe Browsing 等),不影响页面 xhr
    "--disable-sync",                   # 禁账号同步
    "--disable-translate",              # 禁翻译
    "--no-zygote",                      # Linux 省 zygote fork 开销(Windows 忽略)
    "--disable-background-timer-throttling",       # 防后台定时器节流(保证 get_live_info 周期稳定)
    "--disable-renderer-backgrounding",            # 防 renderer 后台降优先级
    "--disable-backgrounding-occluded-windows",    # 防遮挡窗口降级
    "--disable-dev-shm-usage",          # Linux 避 /dev/shm 不足写磁盘(Windows 忽略)
    # rev10: 缓存上限(=1 实质禁用),省 profile 磁盘占用 + 缓存内存映射;不影响 cookie/localStorage
    "--disk-cache-size=1",
    "--media-cache-size=1",
    # 关掉与直播无关的后台特性(投屏/前进后退缓存/预加载/优化提示/ClientHints),省常驻内存
    # (headed/headless 均安全:不影响页面渲染与 stealth)
    "--disable-features=MediaRouter,BackForwardCache,PreloadMediaEngagementData,OptimizationHints,AcceptCHFrame",
    # 限制 renderer 的 V8 老生代堆上限,防止 liveBuild SPA 长跑后 JS 堆只增不减;
    # 512MB 对「仅轮询+状态管理」的页面绰绰有余(DOM/图片等由 Blink 另行分配,不受此限)
    "--js-flags=--max-old-space-size=512",
]

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


async def launch_stealth(playwright, profile_dir, headless=True, window_size=None):
    """启动持久化 Chromium context,带反检测配置。

    登录会话与账号 worker 共用,保证视频号页面看到的环境一致。
    window_size=(w,h):设 headed 浏览器窗口大小(加 --window-size args + viewport);None 用默认 1280x800。
    """
    args = list(STEALTH_ARGS)
    # rev14: headless 模式下禁用 GPU 进程与软件光栅化(后端 context 不渲染可见画面,
    # 禁用安全且每 context 省 50~100MB)。headed 模式(扫码弹窗)保留 GPU,否则窗口无法渲染。
    if headless:
        args.extend([
            "--disable-gpu",
            "--disable-software-rasterizer",
        ])
    if window_size:
        args.append(f"--window-size={window_size[0]},{window_size[1]}")
        viewport = {"width": window_size[0], "height": window_size[1]}
    else:
        viewport = {"width": 1280, "height": 800}
    ctx = await playwright.chromium.launch_persistent_context(
        profile_dir, headless=headless,
        viewport=viewport, args=args, user_agent=DEFAULT_UA)
    await ctx.add_init_script(STEALTH_INIT_SCRIPT)
    logger.debug(f"[browser] launch_stealth headless={headless} profile={profile_dir} window={window_size}")
    return ctx


async def close_context_safely(ctx, profile_dir=None, log_tag=""):
    """关闭持久化 context,吞异常;close 失败时按 profile_dir 兜底 kill 残留 chrome 进程。

    带 FLV 直播流的 context.close 可能抛 "Browser has been closed"/管道异常,
    此时 Chromium 子进程可能未被终止 -> 孤儿 chrome-headless-shell.exe 堆积
    (反复启停后任务管理器里一串进程的根因)。profile_dir 给定时,close 异常后
    按 --user-data-dir 命令行参数匹配 kill 残留进程(持久化 context 启动必带该参数,
    且 profile 锁保证同一 profile_dir 同一时刻只有一个 context,不会误杀)。
    """
    closed_ok = True
    try:
        await ctx.close()
    except Exception as e:
        closed_ok = False
        logger.debug(f"[browser] {log_tag} context.close 异常(将兜底 kill): {e}")
    if closed_ok or not profile_dir:
        return
    try:
        import psutil
    except ImportError:
        logger.warning(f"[browser] {log_tag} context.close 失败且未装 psutil,无法兜底 kill 残留进程")
        return
    abs_dir = os.path.abspath(profile_dir)
    killed = 0
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            for arg in (p.info.get("cmdline") or []):
                if arg.startswith("--user-data-dir="):
                    if os.path.abspath(arg.split("=", 1)[1]) == abs_dir:
                        p.kill()
                        killed += 1
                        break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    if killed:
        logger.info(f"[browser] {log_tag} 兜底 kill {killed} 个残留 chrome 进程(profile={profile_dir})")
