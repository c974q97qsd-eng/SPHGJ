# -*- coding: utf-8 -*-
"""读创作者中心某页面调过的所有接口(递归含同源 iframe)。

思路:
1. HTTP /json/new 直接开一个新标签页到目标 URL(不动用户已有标签页)
2. 只附着这一个 page,不碰浏览器里其它标签
3. 页面内 JS 递归遍历 window/top 及所有同源 iframe,读 performance resource 记录
   —— creator.douyin.com 的微前端 iframe 是同源的,可直接访问 contentWindow

用法: python probe2.py <url> [等待秒数] [名字] [滚动次数]
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

# 页面内采集脚本:递归遍历所有同源 frame,收集接口类资源
JS_COLLECT = r"""
(() => {
  const HINTS = ['/web/api/', '/aweme/v1/', '/janus/', '/live/api/', '/webcast/api/', '/api/'];
  const out = [];
  const seenWin = new Set();
  const walk = (win, path, depth) => {
    if (!win || depth > 5 || seenWin.has(win)) return;
    seenWin.add(win);
    try {
      const href = win.location.href;
      const perf = win.performance;
      if (perf && perf.getEntriesByType) {
        for (const e of perf.getEntriesByType('resource')) {
          const n = e.name || '';
          if (!n.startsWith('http')) continue;
          if (!HINTS.some(h => n.indexOf(h) >= 0)) continue;
          out.push({frame: href.slice(0, 120), url: n, type: e.initiatorType});
        }
      }
      const doc = win.document;
      if (!doc) return;
      const frames = doc.querySelectorAll('iframe');
      for (let i = 0; i < frames.length; i++) {
        try { walk(frames[i].contentWindow, path + '>' + i, depth + 1); } catch (e) {}
      }
    } catch (e) {}
  };
  walk(window, 'top', 0);
  return JSON.stringify({
    title: document.title,
    href: location.href,
    text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 300),
    items: out
  });
})()
"""


def new_tab(url):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def close_tab(tid):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
    except Exception:
        pass


def main():
    url = sys.argv[1]
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    name = sys.argv[3] if len(sys.argv) > 3 else "probe2"
    scroll = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    tab = new_tab(url)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=90)
    try:
        c.call("Runtime.enable")
        c.call("Page.enable")
        # 等加载
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(2)
            try:
                title = c.eval("document.title")
            except Exception:
                title = ""
            if title and title != "抖音创作者中心" or (title and time.time() - t0 > 8):
                break
        # 滚动触发懒加载/分页
        for _ in range(scroll):
            try:
                c.eval("(() => { window.scrollBy(0, 3000);"
                       " document.querySelectorAll('[class*=scroll]').forEach(e => e.scrollTop = e.scrollHeight);"
                       " return 1; })()")
            except Exception:
                pass
            time.sleep(4)
        raw = c.eval(JS_COLLECT)
        info = json.loads(raw) if raw else {"items": []}
    finally:
        c.close()
        close_tab(tid)

    uniq, seen = [], set()
    for it in info.get("items", []):
        hp = it["url"].replace("https://", "").split("?")[0]
        if hp in seen:
            continue
        seen.add(hp)
        uniq.append({**it, "path": hp})

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"url": url, "title": info.get("title"), "href": info.get("href"),
                   "text": info.get("text"), "apis": uniq}, f, ensure_ascii=False, indent=2)

    print("标题:", info.get("title"), "| 落地:", (info.get("href") or "")[:90])
    print("页面文字:", (info.get("text") or "")[:120])
    print(f"\n接口 {len(uniq)} 个 -> {path}")
    for r in uniq:
        print("   %-92s %s" % (r["path"][:92], r["type"]))


if __name__ == "__main__":
    main()
