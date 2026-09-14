"""验证 post/post_update_visible(隐藏/公开)接口。

安全策略:
  * 用**真实 objectId 但发送它当前的 visibleType** —— 等于把状态设成它本来就是的值,
    不产生任何数据变更,却能用真实 ID 验证参数是否完整(非法 ID 会被业务层提前拒绝)。
  * 加 --apply 才会真正切换到相反状态(探测成功后再单独用,并立刻切回)。

已探明的事实(2026-09-15):
  * 接口名: /post/post_update_visible  (前端内部名 updateVisible)
  * 参数  : {objectId, visibleType}  —— 见 js_ctx.txt
  * base  : /micro/content/cgi-bin/mmfinderassistant-bin  (内容管理页;评论页是 micro/interaction)
  * URL   : 必须带 ?_aid=xx&_pageUrl=xx,否则 errCode=300800

产出: .workbuddy/posts_probe/visible_probe.json
"""
import argparse
import asyncio
import io
import json
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/posts_probe"
PAGE_URL = "https://channels.weixin.qq.com/platform/post/list"
BASES = [
    "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin",
    "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin",
]
PATH = "post/post_update_visible"

CALL = r"""async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify(body)
  });
  return {status: r.status, body: (await r.text()).slice(0, 600)};
}"""

GET_IDS = r"""() => {
  const raw = localStorage.getItem('__ml::aid') || localStorage.getItem('__rx::aid') || '';
  let a = raw;
  try { a = JSON.parse(raw); } catch (e) { a = raw.replace(/^"|"$/g, ''); }
  const f = localStorage.getItem('finder_username') || '';
  return {aid: a, fid: f, href: location.href};
}"""


def body_base(finder_id, **extra):
    b = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    b.update(extra)
    return b


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="./profiles/<账号profile>")
    ap.add_argument("--wait", type=int, default=15000)
    ap.add_argument("--apply", action="store_true", help="真正切换状态(会自动切回)")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    report = []

    async with async_playwright() as pw:
        c = await launch_stealth(pw, args.profile)
        page = c.pages[0] if c.pages else await c.new_page()
        # _log_finder_id(finder_username)只在评论页写入,先去那儿取
        try:
            await page.goto("https://channels.weixin.qq.com/micro/interaction/comment",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(6000)
        ids = await page.evaluate(GET_IDS)
        aid, fid = ids["aid"], ids["fid"]
        print(f"_aid={str(aid)[:20]}  _log_finder_id={fid[:30]}")
        if not aid or not fid:
            print("!! 未登录,取不到 aid/fid")
            await c.close()
            return

        # 回到内容管理页(接口同源即可,fetch 自带 cookie)
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        await page.wait_for_timeout(args.wait)

        # 取一个真实作品(用于验证参数完整性),记下它当前的 visibleType
        real = await page.evaluate(r"""async (base) => {
            const r = await fetch(base + '/post/post_list?_aid=' + AID, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify({pageSize: 3, currentPage: 1, userpageType: 11,
                                      stickyOrder: true, timestamp: String(Date.now()),
                                      _log_finder_uin: '', _log_finder_id: FID,
                                      rawKeyBuff: '', pluginSessionId: null, scene: 7, reqScene: 7})
            });
            return JSON.parse(await r.text());
        }""".replace("AID", "'" + str(aid) + "'").replace("FID", "'" + fid + "'"), BASES[0])
        lst = ((real or {}).get("data") or {}).get("list") or []
        if not lst:
            print("!! 取不到作品列表")
            await c.close()
            return
        oid = lst[0].get("exportId") or lst[0].get("objectId")
        cur_vt = lst[0].get("visibleType")
        print(f"样本作品 visibleType={cur_vt}  objectId={str(oid)[:50]}")
        print(f"标题: {(lst[0].get('desc') or {}).get('description', '')[:40]}")

        # 1) 零风险:发送【当前值】(状态不变)
        for base in BASES:
            tag = base.split("channels.weixin.qq.com")[-1]
            url = f"{base}/{PATH}?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
            r = await page.evaluate(CALL, {"url": url,
                                           "body": body_base(fid, objectId=oid, visibleType=cur_vt)})
            ok = '"errCode":0' in r["body"].replace(" ", "")
            print(f"  [当前值 vt={cur_vt}] {tag}\n     -> {r['body'][:200]}")
            report.append({"base": tag, "mode": "current_value", "visibleType": cur_vt,
                           "ok": ok, **r})
            await page.wait_for_timeout(1500)

        # 2) 真正切换(仅 --apply):切换后立即切回
        if args.apply:
            other = 1 if cur_vt != 1 else 2
            url = f"{BASES[0]}/{PATH}?_aid={aid}&_pageUrl={quote(PAGE_URL)}"
            r1 = await page.evaluate(CALL, {"url": url,
                                            "body": body_base(fid, objectId=oid, visibleType=other)})
            print(f"  [切换 -> vt={other}] -> {r1['body'][:200]}")
            report.append({"mode": "switch", "visibleType": other, **r1})
            await page.wait_for_timeout(2500)
            r2 = await page.evaluate(CALL, {"url": url,
                                            "body": body_base(fid, objectId=oid, visibleType=cur_vt)})
            print(f"  [切回 -> vt={cur_vt}] -> {r2['body'][:200]}")
            report.append({"mode": "restore", "visibleType": cur_vt, **r2})

        await c.close()

    with io.open(os.path.join(OUT, "visible_probe.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("-> visible_probe.json")


if __name__ == "__main__":
    asyncio.run(main())
