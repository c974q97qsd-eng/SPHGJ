# -*- coding: utf-8 -*-
"""CDP 抓包 v2:支持 iframe(OOPIF)自动附着。

创作者中心是 iframe 微前端(creator.douyin.com/goofy/douyin_creator_pc/...),
真实接口在子 frame 里发,只附着顶层 page 抓不到 -> 用 Target.setAutoAttach(flatten)
把每一层 frame 都纳入 Network 监听。

用法: python catch_api2.py <url> [等待秒数] [名字] [滚动次数]
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
SKIP = ("security.zijieapi.com", "mon.zijieapi.com", "mcs.zijieapi.com",
        "log.snssdk.com", "mon.snssdk.com", "vcs.zijieapi.com", "slardar",
        "ibytedapm.com", "feelgood.cn", "douyinstatic.com", "douyinpic.com",
        "amemv.com/obj/", "bytednsdoc", "gstatic")


def browser_ws():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=10) as r:
        return json.loads(r.read().decode())["webSocketDebuggerUrl"]


class Catcher:
    def __init__(self, ws_url):
        self.c = CDP(ws_url, timeout=90)
        self.mid = 0
        self.sessions = set()
        self.reqs = []            # [{session,reqid,method,url,type,postData,frame}]
        self.by_id = {}
        self.bodies = {}
        self.frame_url = {}
        self.targets = {}

    # ---- 底层:带 sessionId 的调用 ----
    def send(self, method, session=None, **params):
        self.mid += 1
        pkt = {"id": self.mid, "method": method, "params": params}
        if session:
            pkt["sessionId"] = session
        self.c.ws.send(json.dumps(pkt))
        return self.mid

    def pump(self, seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                raw = self.c.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            self.handle(msg)

    def handle(self, msg):
        m = msg.get("method")
        p = msg.get("params") or {}
        sid = msg.get("sessionId")
        if m == "Target.attachedToTarget":
            child = p.get("sessionId")
            info = p.get("targetInfo") or {}
            if child and child not in self.sessions:
                self.sessions.add(child)
                self.targets[child] = info
                # 自动附着要递归到孙层,并且给每个 session 开 Network
                self.send("Target.setAutoAttach", session=child, autoAttach=True,
                          waitForDebuggerOnStart=False, flatten=True)
                self.send("Network.enable", session=child,
                          maxTotalBufferSize=200 * 1024 * 1024)
                self.send("Runtime.enable", session=child)
        elif m == "Network.requestWillBeSent":
            r = p.get("request") or {}
            u = r.get("url") or ""
            if any(s in u for s in SKIP) or u.startswith("data:"):
                return
            rid = p.get("requestId")
            key = (sid, rid)
            if key in self.by_id:
                return
            rec = {"session": sid, "reqid": rid, "method": r.get("method"),
                   "url": u, "type": p.get("type"),
                   "postData": (r.get("postData") or "")[:2000],
                   "frame": (self.targets.get(sid) or {}).get("url", "")[:80]}
            self.by_id[key] = rec
            self.reqs.append(rec)
        elif m == "Network.loadingFinished":
            key = (sid, p.get("requestId"))
            rec = self.by_id.get(key)
            if not rec:
                return
            self.pending_bodies = getattr(self, "pending_bodies", [])
            self.pending_bodies.append(key)

    def fetch_bodies(self, limit=60):
        keys = getattr(self, "pending_bodies", [])[:limit]
        for key in keys:
            sid, rid = key
            mid = self.send("Network.getResponseBody", session=sid, requestId=rid)
            # 同步等这条响应
            t0 = time.time()
            while time.time() - t0 < 5:
                try:
                    raw = self.c.ws.recv()
                except Exception:
                    break
                msg = json.loads(raw)
                if msg.get("id") == mid:
                    res = msg.get("result") or {}
                    body = res.get("body") or ""
                    if body[:1] in ("{", "[") and not res.get("base64Encoded"):
                        self.bodies[key] = body[:6000]
                    break
                self.handle(msg)


def main():
    url = sys.argv[1]
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    name = sys.argv[3] if len(sys.argv) > 3 else "cap"
    scroll = int(sys.argv[4]) if len(sys.argv) > 4 else 2

    cat = Catcher(browser_ws())
    cat.send("Target.setDiscoverTargets", discover=True)
    cat.send("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
    tid = cat.send  # noqa
    # 新开标签页
    mid = cat.send("Target.createTarget", url="about:blank")
    target_id = None
    t0 = time.time()
    while time.time() - t0 < 10 and not target_id:
        try:
            msg = json.loads(cat.c.ws.recv())
        except Exception:
            break
        if msg.get("id") == mid:
            target_id = (msg.get("result") or {}).get("targetId")
        else:
            cat.handle(msg)
    print("新标签 targetId:", target_id)
    cat.pump(1.5)

    # 找到该 target 的 page session
    page_sid = None
    for sid, info in cat.targets.items():
        if info.get("targetId") == target_id:
            page_sid = sid
            break
    print("page session:", page_sid, "已附着 session 数:", len(cat.sessions))
    if page_sid:
        cat.send("Page.enable", session=page_sid)
        cat.send("Page.navigate", session=page_sid, url=url)
    cat.pump(wait)

    for i in range(scroll):
        for sid, info in list(cat.targets.items()):
            if "creator.douyin.com" not in (info.get("url") or ""):
                continue
            cat.send("Runtime.evaluate", session=sid, expression=(
                "(() => { window.scrollBy(0, 3000);"
                "document.querySelectorAll('[class*=scroll]').forEach(e => e.scrollTop = e.scrollHeight);"
                "return 1; })()"), returnByValue=True)
        cat.pump(4)

    cat.pump(3)
    cat.fetch_bodies()

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url,
                   "requests": cat.reqs,
                   "bodies": {f"{k[0][:8]}|{k[1]}": v for k, v in cat.bodies.items()}},
                  f, ensure_ascii=False, indent=2)

    if target_id:
        try:
            cat.send("Target.closeTarget", targetId=target_id)
            cat.pump(0.5)
        except Exception:
            pass

    print(f"\n捕获 {len(cat.reqs)} 个请求,{len(cat.bodies)} 个 JSON 响应 -> {path}\n")
    only_api = [r for r in cat.reqs if ("/web/api/" in r["url"] or "/aweme/v1/" in r["url"]
                                       or "/api/" in r["url"])]
    print(f"其中业务接口 {len(only_api)} 个:")
    for r in only_api:
        hp = r["url"].replace("https://", "").split("?")[0]
        has = " [JSON]" if any(k[1] == r["reqid"] and k[0] == r["session"] for k in cat.bodies) else ""
        print(f"  {r['method']:6s} {hp[:100]}{has}")
        if r.get("postData"):
            print(f"         body: {r['postData'][:200]}")


if __name__ == "__main__":
    main()
