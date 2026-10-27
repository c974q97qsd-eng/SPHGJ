# -*- coding: utf-8 -*-
"""从 bundle 里抽取所有引号包裹的类路径字符串(补 gap:评论删除/置顶、作品置顶/可见性)。"""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP

PORT = 9222
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
JS = r"""
(async () => {
  const urls = new Set();
  const walk = (win, d) => {
    if (!win || d > 5) return;
    try {
      const doc = win.document;
      if (doc) {
        doc.querySelectorAll('script[src]').forEach(s => urls.add(s.src));
        doc.querySelectorAll('iframe').forEach(f => { try { walk(f.contentWindow, d+1); } catch(e){} });
      }
    } catch (e) {}
  };
  walk(window, 0);
  const out = new Set();
  const re = /["'`](\/[A-Za-z0-9_\/\-{}.:]{4,90})["'`]/g;
  const KEY = /(api|aweme|janus|media|comment|item|upload|publish|reply|top|delete|visib|permission|draft|work)/i;
  let ok = 0, fail = 0;
  for (const src of urls) {
    if (!/creator\.douyin\.com|douyinstatic/.test(src)) continue;
    try {
      const r = await fetch(src, {credentials:'omit'});
      if (!r.ok) { fail++; continue; }
      const t = await r.text(); ok++;
      let m;
      while ((m = re.exec(t)) !== null) { if (KEY.test(m[1])) out.add(m[1]); }
    } catch(e) { fail++; }
  }
  return JSON.stringify({ok, fail, paths: Array.from(out).sort()});
})()
"""
url = sys.argv[1]; name = sys.argv[2] if len(sys.argv) > 2 else "quoted"
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
tab = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
tid = tab["id"]; c = CDP(tab["webSocketDebuggerUrl"], timeout=180)
try:
    c.call("Runtime.enable"); time.sleep(22)
    raw = c.eval(JS, await_promise=True); data = json.loads(raw) if raw else {}
finally:
    c.close()
    try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
    except Exception: pass
os.makedirs(OUT, exist_ok=True)
p = os.path.join(OUT, name + ".json")
json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"抓到 {data.get('ok')} 个脚本,路径 {len(data.get('paths') or [])} 条 -> {p}\n")
for x in data.get("paths") or []:
    print("   " + x)
