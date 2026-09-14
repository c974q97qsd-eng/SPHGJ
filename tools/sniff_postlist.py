"""嗅探「内容管理-视频」页 https://channels.weixin.qq.com/platform/post/list

与 sniff_posts.py 的区别:
  * 直接 goto 到用户指定的正确入口(之前跑的是评论页,拿到的只是评论场景的 post_list)
  * 支持 --scroll 触发分页/懒加载
  * 附带 DOM 分析:把页面上跟「隐藏/仅自己可见/可见/更多/删除」相关的可交互元素
    的文案、class、data-* 都抓下来 -- 用来推断隐藏操作的接口名,**不点击、零风险**

产出: .workbuddy/posts_probe/plist.jsonl / plist.json / dom_ops.txt
用法:
  python tools/sniff_postlist.py --profile ./profiles/xxx [--scroll 3] [--analyze]
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
URL = "https://channels.weixin.qq.com/platform/post/list"
TARGET = "mmfinderassistant-bin"

# 页面上可能出现的操作文案
OP_WORDS = ["隐藏", "仅自己可见", "取消隐藏", "设为公开", "公开可见", "可见",
            "删除", "更多", "置顶", "编辑", "下载", "分享"]

JS_SCAN = r"""
() => {
  const res = [];
  const push = (tag, txt, cls, attrs) => {
    res.push({ tag, txt: (txt || '').trim().slice(0, 40), cls: (cls || '').slice(0, 120), attrs: (attrs || '').slice(0, 200) });
  };
  const nodes = document.querySelectorAll('button, a, li, div[class*="menu"], div[class*="item"], span, div[role="button"]');
  for (const el of nodes) {
    const t = el.textContent || '';
    if (t.length > 60) continue;              // 跳过整块容器
    const hit = WORDS.filter(w => t.includes(w));
    if (!hit.length) continue;
    let attrs = '';
    for (const a of el.attributes) {
      if (a.name.startsWith('data-') || a.name === 'id' || a.name === 'title' || a.name === 'aria-label')
        attrs += a.name + '=' + a.value + ' ';
    }
    push(el.tagName, t, el.className, attrs);
  }
  return res.slice(0, 400);
}
""".replace("WORDS", json.dumps(OP_WORDS, ensure_ascii=False))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--wait", type=int, default=15000)
    ap.add_argument("--scroll", type=int, default=0, help="滚动几次触发分页")
    ap.add_argument("--analyze", action="store_true", help="扫描操作类 DOM 元素")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    jsonl = os.path.join(OUT, "plist.jsonl")
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
                if TARGET not in u or resp.request.method != "POST":
                    return
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
                print(f"  [抓到] {rec['path']:46} {resp.status}  {len(body)}B")
            except Exception:
                pass

        page.on("response", on_resp)

        print(f"1) goto {URL} ...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("   goto 失败:", e)
        await page.wait_for_timeout(args.wait)

        for i in range(args.scroll):
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(2500)
            print(f"   滚动 {i + 1}/{args.scroll}")

        if args.analyze:
            print("2) 扫描操作类 DOM ...")
            try:
                ops = await page.evaluate(JS_SCAN)
                with open(os.path.join(OUT, "dom_ops.txt"), "w", encoding="utf-8") as f:
                    for o in ops:
                        f.write(f"[{o['tag']}] {o['txt']}\n    cls={o['cls']}\n    attrs={o['attrs']}\n")
                print(f"   命中 {len(ops)} 个 -> dom_ops.txt")
            except Exception as e:
                print("   DOM 扫描失败:", e)

        print("\n=== 捕获汇总 ===")
        seen = {}
        for c in captured:
            seen[c["path"]] = seen.get(c["path"], 0) + 1
        for p_, n in sorted(seen.items()):
            print(f"  {p_:52} x{n}")

        with open(f"{OUT}/plist.json", "w", encoding="utf-8") as f:
            json.dump(captured, f, ensure_ascii=False, indent=2)
        print(f"\n共 {len(captured)} 条 -> {OUT}/plist.json")
        fp.close()
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
