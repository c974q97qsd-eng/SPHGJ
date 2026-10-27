# -*- coding: utf-8 -*-
"""从 creator.douyin.com 的 JS bundle 里扫出全部接口路径(只读,不触发任何写操作)。

创作者的写接口(置顶/删除/改可见性/发布)不能拿真实账号去点,但路径字符串都编译进
前端 bundle 了 —— 直接抓包体正则扫出来即可。

用法: python probe_bundle.py <页面url> [名字]
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

JS = r"""
(async () => {
  const collectScripts = () => {
    const urls = new Set();
    const walk = (win, depth) => {
      if (!win || depth > 5) return;
      try {
        const doc = win.document;
        if (doc) {
          doc.querySelectorAll('script[src]').forEach(s => urls.add(s.src));
          doc.querySelectorAll('iframe').forEach(f => { try { walk(f.contentWindow, depth+1); } catch(e){} });
        }
      } catch (e) {}
    };
    walk(window, 0);
    return Array.from(urls);
  };
  const PATTERNS = [
    /\/janus\/[a-z0-9_\/-]{6,80}/gi,
    /\/web\/api\/[a-z0-9_\/-]{4,80}/gi,
    /\/aweme\/v1\/[a-z0-9_\/-]{4,80}/gi,
    /creator-micro\/[a-z0-9_\/-]{3,60}/gi
  ];
  const found = new Set();
  const scripts = collectScripts();
  let okCount = 0, failCount = 0;
  for (const src of scripts.slice(0, 120)) {
    if (!/creator\.douyin\.com|douyinstatic/.test(src)) continue;
    try {
      const r = await fetch(src, {credentials: 'omit'});
      if (!r.ok) { failCount++; continue; }
      const t = await r.text();
      okCount++;
      for (const p of PATTERNS) {
        const ms = t.match(p);
        if (ms) ms.forEach(x => found.add(x.toLowerCase()));
      }
    } catch (e) { failCount++; }
  }
  return JSON.stringify({scriptCount: scripts.length, okCount, failCount,
                         paths: Array.from(found).sort()});
})()
"""


def main():
    url = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "bundle"
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=20) as r:
        tab = json.loads(r.read().decode())
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=180)
    try:
        c.call("Runtime.enable")
        time.sleep(20)
        raw = c.eval(JS, await_promise=True)
        data = json.loads(raw) if raw else {}
    finally:
        c.close()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
        except Exception:
            pass

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"script {data.get('scriptCount')} 个, 抓到 {data.get('okCount')} 个, "
          f"失败 {data.get('failCount')}, 路径 {len(data.get('paths') or [])} 条 -> {path}\n")
    for p in data.get("paths") or []:
        print("   " + p)


if __name__ == "__main__":
    main()
