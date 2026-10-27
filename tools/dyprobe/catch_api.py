# -*- coding: utf-8 -*-
"""在已登录的 9222 浏览器里新开标签页,抓取某个创作者中心页面的全部接口调用。

用法:
    python catch_api.py <url> [等待秒数] [标签名字]

只读操作:新开标签页 -> 加载 -> 可选滚动列表 -> 收集 Network 请求与 JSON 响应 -> 关标签页。
绝不点击发布/删除等写操作按钮。
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
SKIP_HOSTS = ("security.zijieapi.com", "mon.zijieapi.com", "mcs.zijieapi.com",
              "log.snssdk.com", "mon.snssdk.com", "vcs.zijieapi.com",
              "lf-cdn-tos", "sf3-cn.feishucdn", "google", "gstatic")


def new_tab(url="about:blank"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def close_tab(tid):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
    except Exception:
        pass


def catch(url, wait=12, name=None, scroll=2):
    tab = new_tab("about:blank")
    tid, ws = tab["id"], tab["webSocketDebuggerUrl"]
    c = CDP(ws, timeout=60)
    reqs, bodies = [], {}
    try:
        c.call("Network.enable", maxTotalBufferSize=200 * 1024 * 1024)
        c.call("Page.enable")
        c.call("Page.navigate", url=url)
        t0 = time.time()
        seen = set()
        while time.time() - t0 < wait:
            try:
                raw = c.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            msg = json.loads(raw)
            m = msg.get("method")
            p = msg.get("params") or {}
            if m == "Network.requestWillBeSent":
                r = p.get("request") or {}
                u = r.get("url") or ""
                if any(h in u for h in SKIP_HOSTS):
                    continue
                key = (r.get("method"), u.split("?")[0])
                if key in seen:
                    continue
                seen.add(key)
                reqs.append({
                    "id": p.get("requestId"),
                    "method": r.get("method"),
                    "url": u,
                    "type": p.get("type"),
                    "postData": (r.get("postData") or "")[:1500],
                })
            elif m == "Network.loadingFinished":
                rid = p.get("requestId")
                if not any(x["id"] == rid for x in reqs):
                    continue
                try:
                    b = c.call("Network.getResponseBody", requestId=rid)
                    txt = b.get("body") or ""
                    if b.get("base64Encoded"):
                        continue
                    if txt[:1] in ("{", "["):
                        bodies[rid] = txt[:4000]
                except Exception:
                    pass
        # 滚动触发分页
        for _ in range(scroll):
            c.eval("(() => { window.scrollBy(0, 2000); "
                   "const els = document.querySelectorAll('[class*=scroll],[class*=list]');"
                   "els.forEach(e => e.scrollTop = e.scrollHeight); return 1; })()")
            time.sleep(3)
    finally:
        try:
            c.eval("document.title")
        except Exception:
            pass
        c.close()

    os.makedirs(OUT, exist_ok=True)
    tag = name or url.replace("https://", "").replace("/", "_")[:60]
    path = os.path.join(OUT, f"{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "requests": reqs,
                   "bodies": {k: v for k, v in bodies.items()}}, f,
                  ensure_ascii=False, indent=2)
    close_tab(tid)
    return reqs, bodies, path


def brief(reqs, bodies):
    print(f"\n捕获接口 {len(reqs)} 个:")
    for r in reqs:
        host_path = r["url"].replace("https://", "").split("?")[0]
        flag = " [JSON]" if r["id"] in bodies else ""
        print(f"  {r['method']:5s} {host_path[:96]}{flag}")
        if r.get("postData"):
            print(f"        body: {r['postData'][:180]}")


if __name__ == "__main__":
    url = sys.argv[1]
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    name = sys.argv[3] if len(sys.argv) > 3 else None
    reqs, bodies, path = catch(url, wait=wait, name=name)
    brief(reqs, bodies)
    print("\n已存:", path)
