# -*- coding: utf-8 -*-
"""打开作品「设置权限」面板,只列选项,不选择任何项(零副作用)。

看清选项文本后,再决定点哪项去抓真正的权限接口。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/content/manage"

JS_FIND = r"""
((text) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,button,a').forEach(el => {
    if (!vis(el) || norm(el.innerText) !== text) return;
    for (const ch of el.children) { if (norm(ch.innerText) === text) return; }
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 50) });
  });
  return JSON.stringify(out);
})
"""

JS_ALL_TEXT = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,label,li,button,input').forEach(el => {
    if (!vis(el)) return;
    for (const ch of el.children) { if (norm(ch.innerText) === norm(el.innerText)) return; }
    const t = norm(el.innerText);
    const typ = el.getAttribute && el.getAttribute('type');
    if (!t && !typ) return;
    if (t.length > 40) return;
    const r = el.getBoundingClientRect();
    out.push({ t, typ: typ || '', x: Math.round(r.left), y: Math.round(r.top),
               tag: el.tagName });
  });
  const seen = new Set(); const uniq = [];
  for (const o of out) { const k = o.t + '|' + o.y + '|' + o.tag;
    if (!seen.has(k)) { seen.add(k); uniq.push(o); } }
  return JSON.stringify(uniq);
})()
"""


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=150)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待页面加载…")
        time.sleep(26)

        hits = json.loads(c.eval(f"({JS_FIND})('设置权限')") or "[]")
        print("「设置权限」命中:", json.dumps(hits, ensure_ascii=False)[:300])
        if not hits:
            print("!! 没找到"); return
        h = hits[0]
        c.start_collect()
        c.click(h["x"], h["y"])
        time.sleep(2.5)
        c.drain()
        apis = api_requests(c.stop_collect())

        texts = json.loads(c.eval(JS_ALL_TEXT) or "[]")
        print("\n---- 面板/页面文本(按 y 排序,y>150 的可能是弹层)")
        for t in sorted(texts, key=lambda x: (x["y"], x["x"]))[:70]:
            print("   y=%-5d x=%-5d %-6s %-4s %s" % (t["y"], t["x"], t["tag"], t["typ"], t["t"][:36]))

        print("\n---- 期间请求 %d" % len(apis))
        for a in apis[:10]:
            print(f"   {a['method']:5s} {a['url'][:140]}")
            if a.get("postData"):
                print("        body:", a["postData"][:250])

        os.makedirs(OUT, exist_ok=True)
        json.dump({"texts": texts, "apis": apis},
                  open(os.path.join(OUT, "perm_panel.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存 perm_panel.json")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
