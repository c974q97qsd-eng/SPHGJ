"""用已登录会话批量试「账号列表 / 切换账号」API(无需扫码)。

对每个候选路径发 POST,分三类结果:
  OK      errCode=0 且有 data  -> 很可能就是目标 API
  EXIST   非 "Cannot POST" 的报错(接口存在但参数/权限不对)
  MISS    "Cannot POST"(接口不存在)
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

PROFILE = "./profiles/_probe_as_1788087132"
OUT = ".workbuddy/account_api"

PATHS = [
    # 列表类
    "auth/get_finder_account_list", "auth/list_finder_account", "auth/list_finder",
    "auth/get_bind_finder_list", "auth/list_bind_finder", "auth/get_finder_list",
    "auth/finder_list", "auth/get_account_list", "auth/get_all_finder_account",
    "auth/get_multi_finder", "auth/get_auth_finder_list", "auth/get_login_finder_list",
    "auth/get_finder_account", "auth/list_talent_relation_by_bind_uin",
    "auth/get_bind_uin_list", "auth/get_finder_info_list", "auth/multi_finder_list",
    "auth/get_user_finder_list", "auth/get_wx_finder_list", "auth/finder_list_by_uin",
    "auth/get_switch_finder_list", "auth/get_select_finder_list", "auth/list_switch_finder",
    # 切换类
    "auth/switch_finder", "auth/switch_finder_account", "auth/select_finder",
    "auth/set_finder", "auth/login_finder", "auth/switch_account", "auth/change_finder",
    "auth/bind_finder", "auth/switch_login_finder", "auth/finder_switch",
    # 已确认存在的(对照)
    "auth/auth_data", "auth/get_auth_info", "auth/mp_finder_window_init",
]


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, PROFILE, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        info = await page.evaluate("""() => ({
            aid: localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '',
            fid: localStorage.getItem('finder_username') || '',
            url: location.href
        })""")
        print("URL:", info["url"])
        aid = info["aid"]
        try:
            aid = json.loads(aid)
        except Exception:
            aid = aid.strip('"')
        print("AID:", aid[:40], "| FID:", info["fid"][:40])

        results = {"OK": [], "EXIST": [], "MISS": []}
        for path in PATHS:
            try:
                r = await page.evaluate("""async ([path, aid]) => {
                    const base = 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/';
                    const url = base + path +
                        '?_aid=' + encodeURIComponent(aid) +
                        '&_rid=' + Math.random().toString(16).slice(2,10) +
                        '&_pageUrl=' + encodeURIComponent('https://channels.weixin.qq.com/platform');
                    try {
                        const resp = await fetch(url, { method:'POST',
                            headers:{'Content-Type':'application/json'},
                            body:'{}', credentials:'include' });
                        return await resp.text();
                    } catch(e) { return 'FETCH_ERR:' + e.message; }
                }""", [path, aid])
            except Exception as e:
                r = f"EVAL_ERR:{e}"

            if "Cannot POST" in r:
                results["MISS"].append(path)
                tag = "MISS "
            elif r.startswith("FETCH_ERR") or r.startswith("EVAL_ERR"):
                results["EXIST"].append((path, r[:120]))
                tag = "ERR  "
            else:
                try:
                    j = json.loads(r)
                    code = j.get("errCode")
                    if code == 0 and j.get("data"):
                        results["OK"].append((path, r[:600]))
                        tag = ">>OK"
                    else:
                        results["EXIST"].append((path, f"errCode={code} {str(j.get('errMsg'))[:40]}"))
                        tag = "EXIST"
                except Exception:
                    results["EXIST"].append((path, r[:120]))
                    tag = "EXIST"
            print(f"  {tag}  {path}")

        print("\n" + "=" * 60)
        print(f"命中(OK) {len(results['OK'])} 个:")
        for p, body in results["OK"]:
            print(f"\n--- {p}\n{body}")
        print(f"\n接口存在但报错 {len(results['EXIST'])} 个:")
        for p, m in results["EXIST"]:
            print(f"  {p:45} {m}")
        print(f"\n不存在(MISS) {len(results['MISS'])} 个")

        with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        print("\n保持浏览器打开 120s...")
        await asyncio.sleep(120)
        await ctx.close()


asyncio.run(main())
