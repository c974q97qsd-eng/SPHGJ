# -*- coding: utf-8 -*-
"""rev27d: 修 find_video_url 跨站 fetch 失效 + 启动回填历史播放地址。"""
import io
import os

LOCAL = r"D:\SPHGJ"
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# 1) find_video_url: 必须先进 channels 域,否则 new_page() 是 about:blank,
#    跨站 fetch 不带 cookie 且被 CORS 拦,post_list 会返回未登录
add(r"backend\post_fetcher.py",
    '''        page = None
        try:
            page = await worker.context.new_page()
            for pg in range(1, int(max_pages) + 1):
                url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
                r = await page.evaluate(FETCH_JS, {"url": url, "body": _body(
                    self.finder_id, pageSize=ps, currentPage=pg)})''',
    '''        page = None
        try:
            page = await worker.context.new_page()
            # 先落到 channels 域:new_page() 是 about:blank,跨站 fetch 不会带登录 cookie
            try:
                await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.debug(f"[posts] 现取地址前打开列表页失败(继续): {e}")
            for pg in range(1, int(max_pages) + 1):
                url = f"{BASE}/post/post_list?_aid={aidv}&_pageUrl={quote(PAGE_URL)}"
                r = await page.evaluate(FETCH_JS, {"url": url, "body": _body(
                    self.finder_id, pageSize=ps, currentPage=pg,
                    userpageType=11, stickyOrder=True)})''',
    "find_video_url: 先进入域名页")

# 2) 启动时回填历史作品的播放地址(from raw,幂等)
add(r"backend\server.py",
    """manager = AccountManager(config, storage, emit=hub.emit)""",
    """# rev27: 历史作品没有单独存播放地址,但 raw 里有 -> 启动补一次(幂等)
try:
    _nv = storage.backfill_video_urls()
    if _nv:
        logger.info(f"[启动] 回填作品播放地址 {_nv} 条")
except Exception as _e:
    logger.warning(f"[启动] 回填作品播放地址失败(不影响启动): {_e}")
manager = AccountManager(config, storage, emit=hub.emit)""",
    "启动回填播放地址")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")
    import ast
    for f in ("post_fetcher", "server"):
        ast.parse(io.open(os.path.join(LOCAL, "backend", f + ".py"), encoding="utf-8").read())
        print(f"  OK  AST {f}.py")
    print("[DONE]")


if __name__ == "__main__":
    apply()
