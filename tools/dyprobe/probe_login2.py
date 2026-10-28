# -*- coding: utf-8 -*-
"""细查抖音未登录落地页的登录卡片内部结构（找真正的扫码二维码）。

上一版探测的问题：2 秒就 break 了，抓到的 3 个「方形候选」全是落地页装饰图
（284/245/155 px），白卡片里当时还是空的。这版改成：
  · 等 readyState=complete 再等 12~20 秒（二维码是异步拉的）
  · 先定位登录卡片（class 含 login-card 的元素），只在其**内部**找方形元素
  · 把卡片区域单独截图，并把卡片 innerHTML 打印出来（看是 canvas/img/svg/iframe）
  · 找不到就尝试点「我是创作者」再等一轮
"""
import base64
import json
import os
import sys
import time
import urllib.request

import websocket

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
os.makedirs(OUT, exist_ok=True)
URL = "https://creator.douyin.com/creator-micro/home"

JS_CARD = r"""
(() => {
  const out = {href: location.href, ready: document.readyState, cards: [], inner: [], biggest: []};
  const pick = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  // 1) 找登录卡片容器
  const cands = [];
  for (const el of document.querySelectorAll('div[class*="login"],div[class*="Login"]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 200 || r.height < 200) continue;
    cands.push({el, r, cls: (el.className || '').toString()});
  }
  cands.sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height);
  const card = cands.length ? cands[0] : null;
  if (card) out.cards.push({cls: card.cls.slice(0, 120), ...pick(card.el),
                            html: card.el.innerHTML.replace(/\s+/g, ' ').slice(0, 900)});
  // 2) 卡片内部的方形元素（二维码不会超出卡片）
  const scope = card ? card.el : document.body;
  for (const el of scope.querySelectorAll('canvas,img,svg,div,iframe')) {
    const r = el.getBoundingClientRect();
    if (r.width < 70 || r.height < 70) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
    const tag = el.tagName.toLowerCase();
    const square = Math.abs(r.width - r.height) <= Math.max(8, r.width * 0.08);
    const o = {tag, square, ...pick(el),
               cls: (el.className || '').toString().slice(0, 70)};
    if (tag === 'iframe') o.src = (el.src || '').slice(0, 100);
    if (square) out.biggest.push(o);
  }
  out.biggest.sort((a, b) => b.w - a.w);
  out.biggest = out.biggest.slice(0, 10);
  out.hasText = card ? card.el.innerText.replace(/\s+/g, ' ').slice(0, 300) : '';
  return JSON.stringify(out);
})()
"""

JS_CLICK = r"""
(() => {
  const want = ['我是创作者', '扫码登录', '登录'];
  const nodes = [...document.querySelectorAll('div,button,span,a')];
  for (const w of want) {
    const el = nodes.find(e => (e.innerText || '').trim() === w &&
                               e.getBoundingClientRect().width > 20);
    if (el) { el.click(); return 'clicked:' + w; }
  }
  return 'none';
})()
"""


class Browser:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=40, suppress_origin=True,
                                              max_size=64 * 1024 * 1024)
        self.mid = 0

    def send(self, method, session=None, **params):
        self.mid += 1
        mid = self.mid
        msg = {"id": mid, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == mid:
                if "error" in r:
                    raise RuntimeError("%s: %s" % (method, r["error"]))
                return r.get("result")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def dump(br, sess, tag):
    v = br.send("Runtime.evaluate", session=sess, expression=JS_CARD,
                returnByValue=True, awaitPromise=False)
    d = json.loads(v["result"]["value"])
    print("\n--- %s ---" % tag)
    print("  href=%s ready=%s" % (d["href"][:110], d["ready"]))
    for c in d["cards"]:
        print("  卡片 %s @(%d,%d) %dx%d" % (c["cls"][:70], c["x"], c["y"], c["w"], c["h"]))
        print("    文本: %s" % (d["hasText"] or "(空)")[:160])
        print("    innerHTML: %s" % c["html"][:400])
    if not d["cards"]:
        print("  ⚠ 没找到登录卡片容器")
    print("  卡片内方形元素:")
    for o in d["biggest"]:
        print("     %-6s %4dx%-4d @(%d,%d) cls=%-30s %s"
              % (o["tag"], o["w"], o["h"], o["x"], o["y"], o["cls"][:30], o.get("src", "")))
    return d


def main():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=10) as r:
        ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
    br = Browser(ws_url)
    bc_id = br.send("Target.createBrowserContext")["browserContextId"]
    tid = br.send("Target.createTarget", url="about:blank",
                  browserContextId=bc_id)["targetId"]
    sess = br.send("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
    try:
        br.send("Page.enable", session=sess)
        br.send("Runtime.enable", session=sess)
        br.send("Page.navigate", session=sess, url=URL)
        print("等待页面稳定 16s ...")
        time.sleep(16)
        d = dump(br, sess, "初始状态")

        # 卡片空 -> 试点一下「我是创作者」/「扫码登录」
        if not d["biggest"]:
            v = br.send("Runtime.evaluate", session=sess, expression=JS_CLICK,
                        returnByValue=True, awaitPromise=False)
            print("\n  点击结果:", v["result"]["value"])
            print("  再等 12s ...")
            time.sleep(12)
            d = dump(br, sess, "点击后")

        # 卡片区域截图（只截卡片，方便肉眼确认）
        if d["cards"]:
            c = d["cards"][0]
            try:
                shot = br.send("Page.captureScreenshot", session=sess, format="png",
                               clip={"x": c["x"], "y": c["y"], "width": c["w"],
                                     "height": c["h"], "scale": 1})
                p = os.path.join(OUT, "login_card_%d.png" % int(time.time()))
                with open(p, "wb") as f:
                    f.write(base64.b64decode(shot["data"]))
                print("\n  卡片截图 ->", p)
            except Exception as e:
                print("  卡片截图失败 %r" % e)
    finally:
        try:
            br.send("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        try:
            br.send("Target.disposeBrowserContext", browserContextId=bc_id)
            print("隔离上下文已销毁")
        except Exception:
            pass
        br.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
