"""深挖 auth/login_finder 的参数格式(已登录会话,无需扫码)。

试各种 body / query 组合,看返回差异,确定"切换账号"该怎么调。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

PROFILE = "./profiles/_probe_as_1788087132"


async def main():
    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, PROFILE, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        info = await page.evaluate("""() => ({
            aid: localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '',
            fid: localStorage.getItem('finder_username') || ''
        })""")
        aid = info["aid"]
        try:
            aid = json.loads(aid)
        except Exception:
            aid = aid.strip('"')
        fid = info["fid"]
        print(f"AID={aid[:40]}\nFID={fid[:50]}\n")

        cases = [
            ("空 body", {}, {}),
            ("finderUsername=自身", {"finderUsername": fid}, {}),
            ("finder_username=自身", {"finder_username": fid}, {}),
            ("username=自身", {"username": fid}, {}),
            ("finderUin=1", {"finderUin": 1}, {}),
            ("finder_uin=1", {"finder_uin": 1}, {}),
            ("objectId=自身", {"objectId": fid}, {}),
            ("空body+query _log_finder_id", {}, {"_log_finder_id": fid}),
            ("finderUsername=乱值", {"finderUsername": "v2_test_not_exist"}, {}),
        ]

        for tag, body, extra_q in cases:
            r = await page.evaluate("""async ([aid, body, extraQ, fid]) => {
                const base = 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/login_finder';
                let qs = '?_aid=' + encodeURIComponent(aid) +
                         '&_rid=' + Math.random().toString(16).slice(2,10) +
                         '&_pageUrl=' + encodeURIComponent('https://channels.weixin.qq.com/platform');
                for (const k in extraQ) qs += '&' + k + '=' + encodeURIComponent(extraQ[k]);
                try {
                    const resp = await fetch(base + qs, { method:'POST',
                        headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(body), credentials:'include' });
                    return await resp.text();
                } catch(e) { return 'FETCH_ERR:' + e.message; }
            }""", [aid, body, extra_q, fid])
            try:
                j = json.loads(r)
                code, msg = j.get("errCode"), j.get("errMsg")
                data = json.dumps(j.get("data"), ensure_ascii=False)[:220]
                print(f"  {tag:28} errCode={code} msg={str(msg)[:30]}")
                print(f"      data={data}")
            except Exception:
                print(f"  {tag:28} 原始={r[:200]}")

        print("\n保持 60s...")
        await asyncio.sleep(60)
        await ctx.close()


asyncio.run(main())
