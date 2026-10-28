# -*- coding: utf-8 -*-
"""补两个实现前必须确认的事实：

A. 抖音登录态到底由**哪些 cookie** 标记 —— 决定「扫码完成」的轻量判定信号
   （不靠 URL、不靠 DOM）。读的是 9222 上已登录会话的 cookie（只读，不改）。
B. 登录页二维码 img 的 `src` 是什么形态（data:base64 / http 图片 / canvas），
   以及页面有没有独立的二维码请求 URL —— 决定推送二维码用「直接取 src」
   还是「按元素 rect 截图」。
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

# 只关心这几个域，别把用户其它站点的 cookie 打出来
DOMAINS = ("douyin.com",)


class Browser:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=40, suppress_origin=True,
                                              max_size=64 * 1024 * 1024)
        self.mid = 0
        self.events = []

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
            self.events.append(r)

    def drain(self, quiet=0.3, max_sec=8.0):
        t0 = time.time()
        old = self.ws.gettimeout()
        try:
            while time.time() - t0 < max_sec:
                self.ws.settimeout(quiet)
                try:
                    self.events.append(json.loads(self.ws.recv()))
                except Exception:
                    break
        finally:
            try:
                self.ws.settimeout(old)
            except Exception:
                pass

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def part_a(br):
    print("=" * 70)
    print("A. 已登录会话的 douyin cookie 名单（只看名字和域，不打印值）")
    print("=" * 70)
    r = br.send("Storage.getCookies")
    rows = [c for c in (r.get("cookies") or [])
            if any(d in (c.get("domain") or "") for d in DOMAINS)]
    rows.sort(key=lambda c: (c.get("domain") or "", c.get("name") or ""))
    for c in rows:
        v = c.get("value") or ""
        print("  %-24s domain=%-22s len=%-4d httpOnly=%-5s expires=%s"
              % (c.get("name"), c.get("domain"), len(v), c.get("httpOnly"),
                 "session" if not c.get("expires") or c.get("expires") < 0 else "常驻"))
    names = {c.get("name") for c in rows}
    print("\n  cookie 总数: %d" % len(rows))
    for k in ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "uid_tt_ss",
              "sid_guard", "passport_csrf_token", "sid_ucp_v1", "ssid_ucp_v1",
              "LOGIN_STATUS", "store-region", "store-region-src"):
        print("    %-20s %s" % (k, "有" if k in names else "无"))
    return names


JS_QR_SRC = r"""
(() => {
  const out = {href: location.href, ready: document.readyState, imgs: [], canvas: 0, svg: 0};
  let card = null;
  for (const el of document.querySelectorAll('div[class*="login"]')) {
    const r = el.getBoundingClientRect();
    if (r.width >= 300 && r.height >= 300) { card = el; break; }
  }
  const scope = card || document.body;
  for (const el of scope.querySelectorAll('img')) {
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 120) continue;
    if (Math.abs(r.width - r.height) > 20) continue;
    const src = el.getAttribute('src') || '';
    out.imgs.push({
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.x), y: Math.round(r.y),
      srcHead: src.slice(0, 60),
      srcLen: src.length,
      isData: src.startsWith('data:'),
      natural: [el.naturalWidth, el.naturalHeight],
      cls: (el.className || '').toString().slice(0, 60),
    });
  }
  out.canvas = scope.querySelectorAll('canvas').length;
  out.svg = scope.querySelectorAll('svg').length;
  return JSON.stringify(out);
})()
"""


def part_b(br):
    print("\n" + "=" * 70)
    print("B. 登录页二维码 img 的 src 形态 + 是否有独立二维码请求")
    print("=" * 70)
    bc_id = br.send("Target.createBrowserContext")["browserContextId"]
    tid = br.send("Target.createTarget", url="about:blank",
                  browserContextId=bc_id)["targetId"]
    sess = br.send("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
    try:
        br.send("Page.enable", session=sess)
        br.send("Runtime.enable", session=sess)
        br.send("Network.enable", session=sess)
        br.events.clear()
        br.send("Page.navigate", session=sess, url="https://creator.douyin.com/creator-micro/home")
        time.sleep(15)
        br.drain()

        # 从网络事件里找可能的二维码请求
        hits = []
        for e in br.events:
            if e.get("method") != "Network.responseReceived":
                continue
            p = e.get("params") or {}
            resp = p.get("response") or {}
            u = resp.get("url") or ""
            mime = (resp.get("mimeType") or "").lower()
            if "image" in mime or any(k in u.lower() for k in ("qrcode", "qr_code", "qr/")):
                hits.append((mime, u[:120]))
        print("  图片类响应 %d 条（前 8）:" % len(hits))
        for mime, u in hits[:8]:
            print("    %-14s %s" % (mime, u))

        v = br.send("Runtime.evaluate", session=sess, expression=JS_QR_SRC,
                    returnByValue=True, awaitPromise=False)
        d = json.loads(v["result"]["value"])
        print("\n  同源方形大图 %d 个（card 内）:" % len(d["imgs"]))
        for o in d["imgs"]:
            print("    %4dx%-4d @(%d,%d) natural=%s data=%s len=%d head=%s"
                  % (o["w"], o["h"], o["x"], o["y"], o["natural"], o["isData"],
                     o["srcLen"], o["srcHead"]))
        print("  canvas 数=%d  svg 数=%d" % (d["canvas"], d["svg"]))
    finally:
        try:
            br.send("Target.closeTarget", targetId=tid)
        except Exception:
            pass
        try:
            br.send("Target.disposeBrowserContext", browserContextId=bc_id)
        except Exception:
            pass


def main():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=10) as r:
        ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
    br = Browser(ws_url)
    try:
        part_a(br)
        part_b(br)
    finally:
        br.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
