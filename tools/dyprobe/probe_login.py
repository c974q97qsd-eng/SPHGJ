# -*- coding: utf-8 -*-
"""探测抖音创作者中心【未登录】时的登录页形态。

要点：用 CDP 的 Target.createBrowserContext 开一个**隔离上下文**（无 cookie），
所以看到的是真正的未登录页，同时完全不触碰 9222 上已登录的会话。

要回答的问题：
  1. 未登录访问 creator.douyin.com 会落到哪个 URL
  2. 二维码是 canvas 还是 img，多大，什么选择器能稳定定位
  3. 页面是否用 iframe（passport 域）
  4. 登录成功的判定信号（URL 变化？cookie？）
"""
import json
import os
import sys
import time
import urllib.request

import websocket

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
os.makedirs(OUT, exist_ok=True)

TARGETS = [
    "https://creator.douyin.com/",
    "https://creator.douyin.com/creator-micro/home",
]

SURVEY_JS = r"""
(() => {
  const out = {href: location.href, title: document.title, ready: document.readyState};
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width >= 40 && r.height >= 40 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const qr = [];
  for (const el of document.querySelectorAll('canvas,img,svg,div[class*="qr"],div[class*="Qr"],div[class*="QR"]')) {
    if (!el.getBoundingClientRect) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 60) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    // 二维码基本都是正方形
    const square = Math.abs(r.width - r.height) <= Math.max(8, r.width * 0.08);
    qr.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 90),
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.x), y: Math.round(r.y),
      square: square,
      src: el.tagName.toLowerCase() === 'img' ? (el.getAttribute('src') || '').slice(0, 80) : undefined,
    });
  }
  out.squareCandidates = qr.filter(o => o.square).sort((a, b) => b.w - a.w).slice(0, 8);
  out.allBig = qr.slice(0, 12);
  out.iframes = [...document.querySelectorAll('iframe')].map(f => (f.src || '').slice(0, 120));
  out.inputs = [...document.querySelectorAll('input')].map(i => ({
    type: i.type, ph: i.placeholder, name: i.name, cls: (i.className || '').toString().slice(0, 60)
  })).slice(0, 10);
  out.bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 400);
  out.cookies = document.cookie.split(';').map(s => s.trim().split('=')[0]).filter(Boolean).slice(0, 40);
  return JSON.stringify(out);
})()
"""


class Browser:
    """浏览器级 CDP（要支持 sessionId，普通 page 级客户端不够用）。"""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=40,
                                              suppress_origin=True,
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


def main():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=10) as r:
        ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
    br = Browser(ws_url)
    bc_id = None
    tid = None
    try:
        bc_id = br.send("Target.createBrowserContext")["browserContextId"]
        print("隔离上下文:", bc_id)

        for url in TARGETS:
            print("\n" + "=" * 70)
            print("访问:", url)
            print("=" * 70)
            tid = br.send("Target.createTarget", url="about:blank",
                          browserContextId=bc_id)["targetId"]
            sess = br.send("Target.attachToTarget", targetId=tid,
                           flatten=True)["sessionId"]
            br.send("Page.enable", session=sess)
            br.send("Runtime.enable", session=sess)
            br.send("Page.navigate", session=sess, url=url)

            last = None
            for i in range(14):
                time.sleep(2)
                try:
                    v = br.send("Runtime.evaluate", session=sess, expression=SURVEY_JS,
                                returnByValue=True, awaitPromise=False)
                    d = json.loads(v["result"]["value"])
                except Exception as e:
                    print("  %2ds 求值失败 %r" % (i * 2 + 2, e))
                    continue
                last = d
                if d.get("squareCandidates"):
                    print("  %2ds 出现方形候选 %d 个" % (i * 2 + 2, len(d["squareCandidates"])))
                    break
                print("  %2ds href=%s 方形候选 0" % (i * 2 + 2, d.get("href", "")[:70]))

            if not last:
                continue
            print("\n  href   =", last["href"][:130])
            print("  title  =", last["title"][:80])
            print("  ready  =", last["ready"])
            print("  iframes=", last["iframes"][:3])
            print("  inputs =", last["inputs"][:4])
            print("  cookies=", last["cookies"][:20])
            print("  方形候选:")
            for o in last["squareCandidates"]:
                print("     %-5s %4dx%-4d @(%d,%d) cls=%-40s %s"
                      % (o["tag"], o["w"], o["h"], o["x"], o["y"], o["cls"][:40],
                         (o.get("src") or "")[:40]))
            print("  大元素(前 8):")
            for o in last["allBig"][:8]:
                print("     %-5s %4dx%-4d sq=%-5s cls=%s"
                      % (o["tag"], o["w"], o["h"], o["square"], o["cls"][:50]))
            print("  bodyText =", last["bodyText"][:220])

            with open(os.path.join(OUT, "login_%s.json" % url.split("//")[1].split("/")[0]),
                      "w", encoding="utf-8") as f:
                json.dump(last, f, ensure_ascii=False, indent=1)

            # 截一张图（全页），人工复核二维码位置
            try:
                shot = br.send("Page.captureScreenshot", session=sess, format="png")
                p = os.path.join(OUT, "login_%d.png" % int(time.time()))
                with open(p, "wb") as f:
                    import base64
                    f.write(base64.b64decode(shot["data"]))
                print("  截图 ->", p)
            except Exception as e:
                print("  截图失败 %r" % e)

            br.send("Target.closeTarget", targetId=tid)
            tid = None
    finally:
        if tid:
            try:
                br.send("Target.closeTarget", targetId=tid)
            except Exception:
                pass
        if bc_id:
            try:
                br.send("Target.disposeBrowserContext", browserContextId=bc_id)
                print("\n隔离上下文已销毁（cookie 一并丢弃，没碰已登录会话）")
            except Exception as e:
                print("销毁上下文失败 %r" % e)
        br.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
