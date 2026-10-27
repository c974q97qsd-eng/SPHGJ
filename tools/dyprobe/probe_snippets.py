# -*- coding: utf-8 -*-
"""抓 bundle 文本,输出关键词前后片段(看模板字符串拼接出来的接口名)。"""
import json, os, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP

PORT = 9222
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
KEYS = sys.argv[2].split(",") if len(sys.argv) > 2 else ["comment/"]
JS = r"""
(async (KEYS) => {
  const urls = new Set();
  const walk = (win, d) => {
    if (!win || d > 5) return;
    try { const doc = win.document; if (doc) {
      doc.querySelectorAll('script[src]').forEach(s => urls.add(s.src));
      doc.querySelectorAll('iframe').forEach(f => { try { walk(f.contentWindow, d+1); } catch(e){} });
    } } catch(e){}
  };
  walk(window, 0);
  const out = new Set();
  for (const src of urls) {
    if (!/creator\.douyin\.com|douyinstatic/.test(src)) continue;
    try {
      const r = await fetch(src, {credentials:'omit'});
      if (!r.ok) continue;
      const t = await r.text();
      for (const k of KEYS) {
        let i = -1;
        while ((i = t.indexOf(k, i + 1)) !== -1) {
          out.add(t.slice(Math.max(0, i - 70), i + 90).replace(/\s+/g, ' '));
          if (out.size > 400) break;
        }
      }
    } catch(e) {}
  }
  return JSON.stringify({snippets: Array.from(out)});
})(
""" 
url = sys.argv[1]; name = sys.argv[3] if len(sys.argv) > 3 else "snip"
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT")
tab = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
tid = tab["id"]; c = CDP(tab["webSocketDebuggerUrl"], timeout=180)
try:
    c.call("Runtime.enable"); time.sleep(22)
    raw = c.eval(JS + json.dumps(KEYS) + ")", await_promise=True)
    data = json.loads(raw) if raw else {}
finally:
    c.close()
    try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
    except Exception: pass
os.makedirs(OUT, exist_ok=True)
p = os.path.join(OUT, name + ".json")
json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"片段 {len(data.get('snippets') or [])} 条 -> {p}\n")
for s in data.get("snippets") or []:
    print("   … " + s[:170])
