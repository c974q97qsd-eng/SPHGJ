# -*- coding: utf-8 -*-
"""CDP 读接口 v3:打开页面,等它自然加载完,再从【每个 frame】的
performance.getEntriesByType('resource') 里读出它调过的所有接口。

比 Network 事件流可靠:不会因附着时机晚而漏抓,也不会被其他标签页污染。

用法: python probe.py <url> [等待秒数] [名字] [滚动次数]
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP  # noqa: E402

PORT = 9222
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
API_HINT = ("/web/api/", "/aweme/v1/", "/api/", "/janus/", "/live/api/", "/webcast/api/")


def browser_ws():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=10) as r:
        return json.loads(r.read().decode())["webSocketDebuggerUrl"]


class Probe:
    def __init__(self):
        self.c = CDP(browser_ws(), timeout=90)
        self.mid = 0
        self.sessions = {}   # sid -> targetInfo

    def send(self, method, session=None, **params):
        self.mid += 1
        pkt = {"id": self.mid, "method": method, "params": params}
        if session:
            pkt["sessionId"] = session
        self.c.ws.send(json.dumps(pkt))
        return self.mid

    def call(self, method, session=None, timeout=20, **params):
        mid = self.send(method, session, **params)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                msg = json.loads(self.c.ws.recv())
            except Exception:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    return {"__error": msg["error"]}
                return msg.get("result") or {}
            self.handle(msg)
        return {}

    def handle(self, msg):
        m, p = msg.get("method"), (msg.get("params") or {})
        if m == "Target.attachedToTarget":
            child = p.get("sessionId")
            info = p.get("targetInfo") or {}
            if child and child not in self.sessions:
                self.sessions[child] = info
                self.send("Target.setAutoAttach", session=child, autoAttach=True,
                          waitForDebuggerOnStart=False, flatten=True)
                self.send("Runtime.enable", session=child)

    def pump(self, seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                msg = json.loads(self.c.ws.recv())
            except Exception:
                break
            self.handle(msg)

    def eval(self, session, expr):
        r = self.call("Runtime.evaluate", session=session, expression=expr,
                      returnByValue=True, awaitPromise=True)
        return (r.get("result") or {}).get("value")


JS_RES = """(() => performance.getEntriesByType('resource').map(e => ({
    name: e.name, type: e.initiatorType, size: e.transferSize || 0
})).filter(x => x.name.startsWith('http') &&
    %s.some(k => x.name.indexOf(k) >= 0)))()""" % (list(API_HINT),)


def main():
    url = sys.argv[1]
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    name = sys.argv[3] if len(sys.argv) > 3 else "probe"
    scroll = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    pb = Probe()
    pb.send("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
    res = pb.call("Target.createTarget", url="about:blank")
    tid = res.get("targetId")
    pb.pump(1.5)

    page_sid = next((s for s, i in pb.sessions.items() if i.get("targetId") == tid), None)
    print("targetId:", tid, " page session:", page_sid)
    if page_sid:
        pb.send("Page.enable", session=page_sid)
        pb.call("Page.navigate", session=page_sid, url=url)

    pb.pump(wait)
    for _ in range(scroll):
        for sid, info in list(pb.sessions.items()):
            if "creator.douyin.com" in (info.get("url") or ""):
                pb.eval(sid, "(() => { window.scrollBy(0, 3000);"
                             "document.querySelectorAll('[class*=scroll]').forEach(e => e.scrollTop = e.scrollHeight);"
                             "return 1; })()")
        pb.pump(4)
    pb.pump(2)

    frames, allres = [], []
    for sid, info in list(pb.sessions.items()):
        u = info.get("url") or ""
        frames.append({"session": sid[:8], "type": info.get("type"), "url": u[:120]})
        if "douyin" not in u:
            continue
        val = pb.eval(sid, JS_RES)
        for item in (val or []):
            allres.append({"frame": u[:110], **item})

    # 去重(按 path)
    uniq, seen = [], set()
    for r in allres:
        key = r["name"].replace("https://", "").split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        uniq.append({**r, "path": key})

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "frames": frames, "apis": uniq}, f, ensure_ascii=False, indent=2)

    if tid:
        pb.call("Target.closeTarget", targetId=tid)

    print(f"\nframe 数 {len(frames)},接口 {len(uniq)} 个 -> {path}")
    for f_ in frames:
        if "douyin" in f_["url"]:
            print("   frame:", f_["url"][:110])
    print()
    for r in uniq:
        print("   %-88s %s" % (r["path"][:88], r.get("type")))


if __name__ == "__main__":
    main()
