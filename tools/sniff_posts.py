"""嗅探:让官方前端自己调作品接口,我们只录请求/响应(零额外请求)。

思路:不做构造式探测(避免 errCode=300334 这类参数/权限坑),只监听页面自身发的
mmfinderassistant-bin 请求,把 URL + 请求体 + 响应体落盘。这样拿到的字段 =
官方前端真实在用的字段,且**不消耗额外请求配额**。

v2 改动:
  * 响应**完整保存**(v1 截断 6KB,作品列表动辄 20KB+,会被截坏)
  * **实时落盘**(jsonl),中途崩溃也不丢数据
  * 支持 --click 进入「内容管理」等子页面

⚠️ 前提:**必须先停掉后端引擎**(POST /api/engine/stop),否则 profile 被独占,
   Playwright 起的是空会话(没有登录态)且会 TargetClosedError 崩溃。

产出:.workbuddy/posts_probe/sniff.jsonl / sniff.json
用法:
  python tools/sniff_posts.py --profile ./profiles/xxx [--click 内容管理]
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
TARGET = "mmfinderassistant-bin"
INTEREST = ("post_list", "post/", "object", "feed", "data", "visible", "del",
            "statistic", "collection")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--click", default="", help="要点击的菜单文字,如 内容管理")
    ap.add_argument("--wait", type=int, default=12000)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    jsonl = os.path.join(OUT, "sniff.jsonl")
    if os.path.exists(jsonl):
        os.remove(jsonl)
    fp = open(jsonl, "a", encoding="utf-8")
    captured = []

    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, args.profile, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_resp(resp):
            try:
                u = resp.url
                if TARGET not in u:
                    return
                if resp.request.method != "POST":
                    return
                body = ""
                try:
                    body = await resp.text()
                except Exception:
                    body = "<body unavailable>"
                rec = {
                    "url": u,
                    "path": u.split(TARGET)[-1].split("?")[0].lstrip("/"),
                    "req": resp.request.post_data or "",
                    "resp": body,
                    "status": resp.status,
                }
                captured.append(rec)
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fp.flush()
                print(f"  [抓到] {rec['path']:44} {resp.status}  {len(body)}B")
            except Exception:
                pass

        page.on("response", on_resp)

        print("1) 打开助手首页 platform ...")
        try:
            await page.goto("https://channels.weixin.qq.com/platform",
                            wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print("   goto platform 失败:", e)
        await page.wait_for_timeout(args.wait)

        if args.click:
            print(f"2) 点击菜单「{args.click}」...")
            try:
                el = page.locator(f"text={args.click}").first
                if await el.count():
                    await el.click(timeout=6000)
                    print("   已点击")
                else:
                    print("   未找到该菜单")
            except Exception as e:
                print("   点击失败:", e)
            await page.wait_for_timeout(args.wait)

        print("3) 打开评论页(项目在用的 post_list 场景)...")
        try:
            await page.goto("https://channels.weixin.qq.com/micro/interaction/comment",
                            wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print("   goto comment 失败:", e)
        await page.wait_for_timeout(args.wait)

        print("\n=== 捕获汇总 ===")
        seen = {}
        for c in captured:
            seen[c["path"]] = seen.get(c["path"], 0) + 1
        for p_, n in sorted(seen.items()):
            mark = "*" if any(k in p_ for k in INTEREST) else " "
            print(f" {mark} {p_:52} x{n}")

        with open(f"{OUT}/sniff.json", "w", encoding="utf-8") as f:
            json.dump(captured, f, ensure_ascii=False, indent=2)
        print(f"\n共 {len(captured)} 条,已写 {OUT}/sniff.json")
        fp.close()

        print("保持 10s ...")
        await asyncio.sleep(10)
        await ctx.close()


asyncio.run(main())
