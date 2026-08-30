"""探测「扫码后选择视频号」页面结构 v3(带负例自检,不浪费扫码机会)。

关键改进:
  1. 负例自检: 先在登录页验证 is_select_page() 必须为 False,
     若误判则立即报错退出(避免用用户扫码来验证探测逻辑)
  2. 标记词只保留选择页独有文案
  3. 全量捕获 cgi-bin 响应体(事后分析账号列表 API)
  4. 命令文件机制: dump 后浏览器保持打开,写 cmd.txt 可反复测试点击,
     无需重复扫码
     - dump            重新 dump
     - click:<index>   点击第 index 个账号项
     - clicktext:<txt> 按文本点击
     - q               退出

产物: .workbuddy/account_select3/
  run.log          运行日志(实时)
  full.html        选择页完整 DOM
  items.json       枚举到的全部账号项
  struct.json      名称节点/勾选框/按钮
  select_page.png  截图
  responses/       全部 cgi-bin 响应体
  net.txt          请求清单
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from backend.browser import launch_stealth

OUT = ".workbuddy/account_select3"
# 干净 profile:确保出现「选择账号」页(已登录 profile 会被直接送进平台)
PROFILE = "./profiles/_probe_as_fresh"
LOGIN = "https://channels.weixin.qq.com/platform/login"
CMD = f"{OUT}/cmd.txt"

# 仅「选择页独有」文案(登录页也有「登录视频号助手」,绝不能用作标记)
MARKERS = ["选择视频号登录", "使用其他账号登录"]
# 登录页特征文案(用于负例自检)
LOGIN_MARKERS = ["扫码登录", "使用微信扫码登录", "登录视频号助手"]

t0 = time.time()
os.makedirs(OUT, exist_ok=True)
os.makedirs(f"{OUT}/responses", exist_ok=True)
_logf = open(f"{OUT}/run.log", "w", encoding="utf-8")


def log(m):
    line = f"[{time.time() - t0:6.1f}s] {m}"
    print(line, flush=True)
    _logf.write(line + "\n")
    _logf.flush()


async def page_text(page):
    try:
        return await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return ""


async def is_select_page(page):
    txt = await page_text(page)
    return any(m in txt for m in MARKERS)


async def dump_all(page, tag=""):
    """到达选择页后的完整 dump。"""
    url = page.url
    log(f"=== 选择页 dump{tag} URL={url} ===")

    html = await page.evaluate("() => document.documentElement.outerHTML")
    with open(f"{OUT}/full.html", "w", encoding="utf-8") as f:
        f.write(html)
    log(f"full.html {len(html)} 字符")

    txt = await page_text(page)
    with open(f"{OUT}/text.txt", "w", encoding="utf-8") as f:
        f.write(txt)
    log(f"页面文本前120字: {txt[:120]!r}")

    try:
        await page.screenshot(path=f"{OUT}/select_page.png", timeout=8000)
        log("截图 select_page.png")
    except Exception as e:
        log(f"截图失败: {e}")

    # 滚动容器
    sc = await page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('*').forEach(el => {
            if (el.scrollHeight > el.clientHeight + 20 && el.clientHeight > 60) {
                const cls = (el.className && el.className.baseVal !== undefined
                             ? el.className.baseVal : el.className) || '';
                out.push({ tag: el.tagName.toLowerCase(), cls: String(cls).slice(0,80),
                           id: el.id||'', sh: el.scrollHeight, ch: el.clientHeight });
            }
        });
        return out.slice(0, 15);
    }""")
    log(f"滚动容器: {json.dumps(sc, ensure_ascii=False)[:300]}")

    # 滚动枚举账号项
    items = await page.evaluate("""async () => {
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const scrollAll = async () => {
            const els = [...document.querySelectorAll('*')].filter(
                el => el.scrollHeight > el.clientHeight + 20 && el.clientHeight > 60);
            for (const el of els) {
                const step = Math.max(120, el.clientHeight);
                for (let y = 0; y < el.scrollHeight; y += step) {
                    el.scrollTop = y; await sleep(100);
                }
                el.scrollTop = el.scrollHeight; await sleep(250);
                el.scrollTop = 0; await sleep(150);
            }
        };
        await scrollAll(); await scrollAll();

        const out = [], seen = new Set();
        document.querySelectorAll('div,li,label,button,tr').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 150 || r.height < 30) return;
            const img = el.querySelector('img');
            const txt = (el.innerText || '').trim().replace(/\\s+/g, ' ');
            if (!txt || txt.length > 80) return;
            if (!img) return;
            const key = txt;
            if (seen.has(key)) return;
            seen.add(key);
            const getCls = e => (e && e.className && e.className.baseVal !== undefined
                                 ? e.className.baseVal : (e && e.className)) || '';
            out.push({
                txt, cls: String(getCls(el)).slice(0,100),
                parentCls: String(getCls(el.parentElement)).slice(0,100),
                grandCls: String(getCls(el.parentElement && el.parentElement.parentElement)).slice(0,100),
                tag: el.tagName.toLowerCase(),
                y: Math.round(r.y + window.scrollY), h: Math.round(r.height), w: Math.round(r.width),
                imgSrc: (img.getAttribute('src') || '').slice(0,140),
                dataAttrs: Object.fromEntries(Object.entries(el.dataset||{})
                    .map(([k,v]) => [k, String(v).slice(0,100)])),
            });
        });
        return out;
    }""")
    with open(f"{OUT}/items.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    log(f"枚举账号项 {len(items)} 个:")
    for i, it in enumerate(items):
        log(f"   [{i}] {it['txt']!r}")
        log(f"        cls={it['cls'][:60]!r} parent={it['parentCls'][:60]!r}")
        if it.get("dataAttrs"):
            log(f"        data={json.dumps(it['dataAttrs'], ensure_ascii=False)[:160]}")

    # 结构: 名称节点 / 勾选框 / 按钮 / 全部 img
    struct = await page.evaluate("""() => {
        const getCls = e => (e.className && e.className.baseVal !== undefined
                             ? e.className.baseVal : e.className) || '';
        const res = { nameNodes: [], checkbox: [], buttons: [], imgs: [] };
        document.querySelectorAll('span,div,p,li,td,label').forEach(el => {
            const t = (el.innerText || '').trim();
            if (!t || t.length > 40 || t.includes('\\n')) return;
            if (el.children.length > 0) return;
            res.nameNodes.push({ txt: t.slice(0,40), cls: String(getCls(el)).slice(0,80),
                                 tag: el.tagName.toLowerCase(),
                                 parentCls: String(getCls(el.parentElement)).slice(0,80) });
        });
        res.nameNodes = res.nameNodes.slice(0, 100);
        document.querySelectorAll('input,[class*="check"],[class*="radio"]').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width < 1) return;
            res.checkbox.push({ tag: el.tagName.toLowerCase(), type: el.getAttribute('type')||'',
                                cls: String(getCls(el)).slice(0,80),
                                x: Math.round(r.x), y: Math.round(r.y),
                                w: Math.round(r.width), h: Math.round(r.height) });
        });
        res.checkbox = res.checkbox.slice(0, 40);
        document.querySelectorAll('button,a,[class*="btn"],[class*="confirm"]').forEach(el => {
            const t = (el.innerText || '').trim().replace(/\\s+/g,' ');
            if (!t) return;
            res.buttons.push({ txt: t.slice(0,30), cls: String(getCls(el)).slice(0,80),
                               tag: el.tagName.toLowerCase() });
        });
        res.buttons = res.buttons.slice(0, 40);
        document.querySelectorAll('img').forEach(el => {
            res.imgs.push({ src: (el.getAttribute('src')||'').slice(0,140),
                            cls: String(getCls(el)).slice(0,60),
                            alt: el.getAttribute('alt')||'' });
        });
        res.imgs = res.imgs.slice(0, 60);
        return res;
    }""")
    with open(f"{OUT}/struct.json", "w", encoding="utf-8") as f:
        json.dump(struct, f, ensure_ascii=False, indent=2)
    log(f"结构: 名称{len(struct['nameNodes'])} 勾选{len(struct['checkbox'])} "
        f"按钮{len(struct['buttons'])} 图片{len(struct['imgs'])}")
    for b in struct["buttons"][:12]:
        log(f"   btn {b['txt']!r} cls={b['cls'][:40]!r}")
    return items


async def cmd_loop(page, items):
    """命令文件驱动: 一次扫码反复验证点击。"""
    last_cmd = None
    for _ in range(1800):  # 最多 30 分钟
        await asyncio.sleep(1)
        if not os.path.exists(CMD):
            continue
        try:
            raw = open(CMD, encoding="utf-8").read().strip()
        except Exception:
            continue
        if not raw or raw == last_cmd:
            continue
        last_cmd = raw
        log(f">>> 收到命令: {raw}")
        try:
            if raw == "q":
                log("退出")
                return
            elif raw == "dump":
                items = await dump_all(page, "(cmd)")
            elif raw.startswith("click:"):
                idx = int(raw.split(":", 1)[1])
                if idx < len(items):
                    it = items[idx]
                    log(f"点击 [{idx}] {it['txt']!r}")
                    sel = f"text={it['txt']}" if it["txt"] else None
                    # 用坐标点击最内层文本节点所在元素
                    ok = await page.evaluate("""(txt) => {
                        const els = [...document.querySelectorAll('div,li,label,span')];
                        for (const el of els) {
                            if ((el.innerText||'').trim().replace(/\\s+/g,' ') === txt)
                                { el.click(); return true; }
                            const c = [...el.children].find(
                                c => (c.innerText||'').trim().replace(/\\s+/g,' ') === txt);
                            if (c) { c.click(); return true; }
                        }
                        return false;
                    }""", it["txt"])
                    log(f"  点击结果={ok}")
                    await asyncio.sleep(3)
                    log(f"  点击后 URL={page.url} 文本={ (await page_text(page))[:100]!r}")
                else:
                    log(f"索引越界(共{len(items)})")
            elif raw.startswith("loginfinder:"):
                fid = raw.split(":", 1)[1]
                log(f"调 login_finder(finderUsername={fid[:30]}...)")
                r = await page.evaluate("""async (fid) => {
                    const aid = localStorage.getItem('__ml::aid') ||
                                localStorage.getItem('__rx::aid') || '';
                    let a = aid; try { a = JSON.parse(aid); } catch(e) { a = aid.replace(/^"|"$/g,''); }
                    const base = 'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/login_finder';
                    const qs = '?_aid=' + encodeURIComponent(a) +
                               '&_rid=' + Math.random().toString(16).slice(2,10) +
                               '&_pageUrl=' + encodeURIComponent(location.href);
                    try {
                        const resp = await fetch(base + qs, { method:'POST',
                            headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({ finderUsername: fid }),
                            credentials:'include' });
                        return await resp.text();
                    } catch(e) { return 'FETCH_ERR:' + e.message; }
                }""", fid)
                log(f"  返回: {r[:300]}")
                await asyncio.sleep(3)
                log(f"  之后 URL={page.url}")
                log(f"  之后文本: {(await page_text(page))[:150]!r}")
            elif raw.startswith("clicktext:"):
                t = raw.split(":", 1)[1]
                ok = await page.evaluate("""(txt) => {
                    const els = [...document.querySelectorAll('div,li,label,span,button')];
                    for (const el of els) {
                        const s = (el.innerText||'').trim().replace(/\\s+/g,' ');
                        if (s === txt || s.includes(txt)) { el.click(); return s; }
                    }
                    return false;
                }""", t)
                log(f"  按文本点击结果={ok}")
                await asyncio.sleep(3)
                log(f"  点击后 URL={page.url}")
            else:
                log(f"未知命令: {raw}")
        except Exception as e:
            log(f"命令执行异常: {e}")
    log("命令循环超时退出")


async def main():
    net_f = open(f"{OUT}/net.txt", "w", encoding="utf-8")
    resp_idx = [0]

    async with async_playwright() as pw:
        log("启动 headed 浏览器")
        ctx = await launch_stealth(pw, PROFILE, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_resp(resp):
            u = resp.url
            try:
                m = resp.request.method
            except Exception:
                m = "?"
            net_f.write(f"RESP {resp.status} {m} {u[:200]}\n")
            net_f.flush()
            if "cgi-bin" in u or "mmfinder" in u:
                try:
                    body = await resp.text()
                except Exception:
                    return
                if len(body) > 400000:
                    return
                resp_idx[0] += 1
                with open(f"{OUT}/responses/r{resp_idx[0]:03d}.txt", "w", encoding="utf-8") as f:
                    f.write(f"// {resp.status} {m} {u}\n{body}")
                log(f"  >> API #{resp_idx[0]} {resp.status} {u[:100]} ({len(body)}B)")

        page.on("response", on_resp)

        log(f"打开登录页 {LOGIN}")
        try:
            await page.goto(LOGIN, wait_until="domcontentloaded")
        except Exception as e:
            log(f"goto 失败(忽略): {e}")
        await page.wait_for_timeout(4000)
        log(f"当前 URL: {page.url}")

        # ===== 负例自检 =====
        txt = await page_text(page)
        sel = await is_select_page(page)
        is_login = any(m in txt for m in LOGIN_MARKERS)
        log(f"负例自检: is_select_page={sel} (必须为 False) / 登录页特征={is_login}")
        if sel:
            log("!!! 自检失败: 登录页被误判为选择页,标记词有问题,终止(不浪费扫码)")
            log(f"页面文本: {txt[:200]!r}")
            net_f.close()
            await ctx.close()
            return
        if not is_login:
            log("!!! 自检警告: 当前页不像登录页,请确认窗口状态")
        log("自检通过")

        # ===== 等待扫码 =====
        log("=" * 56)
        log(">>> 请用微信扫描浏览器窗口二维码 <<<")
        log(">>> 扫码后在手机上点确认 <<<")
        log("=" * 56)
        found = False
        for _ in range(900):  # 7.5 分钟
            await asyncio.sleep(0.5)
            if await is_select_page(page):
                found = True
                break
        if not found:
            log("未检测到选择页(可能该微信只绑1个视频号,直接进入平台)")
            log(f"当前 URL={page.url}")
            log(f"文本: {(await page_text(page))[:200]!r}")
            with open(f"{OUT}/full.html", "w", encoding="utf-8") as f:
                f.write(await page.evaluate("() => document.documentElement.outerHTML"))
            try:
                await page.screenshot(path=f"{OUT}/after_scan.png", timeout=8000)
            except Exception:
                pass
            net_f.close()
            await ctx.close()
            return

        log("检测到账号选择页!")
        await page.wait_for_timeout(2000)
        items = await dump_all(page)

        log("=" * 56)
        log("dump 完成。浏览器保持打开,可写 cmd.txt 反复测试点击:")
        log("   dump / click:<index> / clicktext:<文本> / q")
        log(f"   命令文件: {os.path.abspath(CMD)}")
        log("=" * 56)
        if os.path.exists(CMD):
            os.remove(CMD)

        await cmd_loop(page, items)
        net_f.close()
        await ctx.close()


asyncio.run(main())
