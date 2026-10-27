# -*- coding: utf-8 -*-
"""抓单个新标签页里指定接口的【完整请求 + 响应】。

用法: python catch_detail.py <url> <路径关键词> [等待秒数] [名字] [滚动次数]

只读:新开标签页 -> 加载 -> 滚动 -> 收集匹配关键词的请求(含 header/postData)
     与响应体 -> 关标签页。
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


def new_tab(url):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def close_tab(tid):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
    except Exception:
        pass


def run(url, needle, wait=25, name="detail", scroll=3):
    tab = new_tab("about:blank")
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=90)
    hits, bodies = [], {}
    try:
        c.call("Network.enable", maxTotalBufferSize=200 * 1024 * 1024)
        c.call("Page.enable")
        c.call("Page.navigate", url=url)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                raw = c.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            msg = json.loads(raw)
            m, p = msg.get("method"), (msg.get("params") or {})
            if m == "Network.requestWillBeSent":
                r = p.get("request") or {}
                u = r.get("url") or ""
                if needle not in u:
                    continue
                hdr = {k: v for k, v in (r.get("headers") or {}).items()
                       if k.lower() in ("content-type", "x-secsdk-csrf-token", "referer",
                                        "x-tt-trace-id", "cookie", "user-agent", "x-bogus",
                                        "x-tt-session-did", "x-tt-trace-log")}
                hits.append({"reqid": p.get("requestId"), "method": r.get("method"),
                             "url": u, "postData": r.get("postData"),
                             "headers": hdr})
            elif m == "Network.loadingFinished":
                rid = p.get("requestId")
                if not any(h["reqid"] == rid for h in hits):
                    continue
                try:
                    b = c.call("Network.getResponseBody", requestId=rid)
                    txt = b.get("body") or ""
                    if txt[:1] in ("{", "["):
                        bodies[rid] = txt
                except Exception:
                    pass
        for _ in range(scroll):
            try:
                c.eval("(() => { window.scrollBy(0, 3000);"
                       "document.querySelectorAll('[class*=scroll]').forEach(e => e.scrollTop = e.scrollHeight);"
                       "return 1; })()")
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 4:
                try:
                    raw = c.ws.recv()
                except Exception:
                    break
                msg = json.loads(raw)
                m, p = msg.get("method"), (msg.get("params") or {})
                if m == "Network.requestWillBeSent":
                    r = p.get("request") or {}
                    u = r.get("url") or ""
                    if needle in u:
                        hits.append({"reqid": p.get("requestId"), "method": r.get("method"),
                                     "url": u, "postData": r.get("postData"),
                                     "headers": {k: v for k, v in (r.get("headers") or {}).items()
                                                 if k.lower() in ("content-type", "referer")}})
                elif m == "Network.loadingFinished":
                    rid = p.get("requestId")
                    if any(h["reqid"] == rid for h in hits):
                        try:
                            b = c.call("Network.getResponseBody", requestId=rid)
                            t = b.get("body") or ""
                            if t[:1] in ("{", "["):
                                bodies[rid] = t
                        except Exception:
                            pass
    finally:
        c.close()
        close_tab(tid)

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "needle": needle, "hits": hits, "bodies": bodies},
                  f, ensure_ascii=False, indent=2)
    return hits, bodies, path


if __name__ == "__main__":
    url, needle = sys.argv[1], sys.argv[2]
    wait = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    name = sys.argv[4] if len(sys.argv) > 4 else "detail"
    scroll = int(sys.argv[5]) if len(sys.argv) > 5 else 2
    hits, bodies, path = run(url, needle, wait, name, scroll)
    print(f"命中 {len(hits)} 个请求,{len(bodies)} 个响应 -> {path}\n")
    for h in hits:
        print(f"{h['method']} {h['url'][:170]}")
        if h.get("headers"):
            print("   headers:", json.dumps(h["headers"], ensure_ascii=False)[:300])
        if h.get("postData"):
            print("   body:", h["postData"][:500])
        b = bodies.get(h["reqid"])
        if b:
            print("   resp:", b[:900].replace("\n", " "))
        print()
