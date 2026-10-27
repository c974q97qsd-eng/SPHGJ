# -*- coding: utf-8 -*-
"""探作品管理页: 点开第一个作品的「...」菜单,只列出菜单项,不点任何具体操作。

产出菜单文本 -> 再决定点哪项去抓对应接口。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/content/manage"

# 列出页面上所有短文本元素(找"..."入口 / 操作栏)
JS_LIST = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,button,i,svg,a').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    const cls = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (typeof el.className === 'string' ? el.className : '');
    // 只收: 短文本 或 明确的更多按钮类名
    const isMore = /more|ellipsis|dropdown|operate|action/i.test(String(cls));
    if (!isMore && (!t || t.length > 10)) return;
    const r = el.getBoundingClientRect();
    out.push({ text: t, cls: String(cls).slice(0, 70), tag: el.tagName,
               x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height) });
  });
  const seen = new Set(); const uniq = [];
  for (const o of out) { const k = o.tag + '|' + o.text + '|' + o.cls + '|' + o.y;
    if (!seen.has(k)) { seen.add(k); uniq.push(o); } }
  return JSON.stringify(uniq.slice(0, 120));
})()
"""

# 点开菜单后,列出所有短文本(即菜单项)
JS_MENU = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,li,button').forEach(el => {
    if (!vis(el)) return;
    for (const ch of el.children) { if (norm(ch.innerText) === norm(el.innerText)) return; }
    const t = norm(el.innerText);
    if (!t || t.length > 12) return;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10) return;
    out.push({ text: t, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) });
  });
  const seen = new Set(); const uniq = [];
  for (const o of out) { const k = o.text + '|' + o.y + '|' + o.x;
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
        print("等待作品管理页加载…")
        time.sleep(26)

        items = json.loads(c.eval(JS_LIST) or "[]")
        print("页面短文本/更多按钮候选 %d 个:" % len(items))
        for it in items[:70]:
            print("   %-5s %-14s %-50s (%d,%d)" % (it["tag"], it["text"][:14], it["cls"][:50], it["x"], it["y"]))

        # 优先找一个明确的 more/ellipsis 类名元素
        more = [it for it in items
                if re.search(r"more|ellipsis|dropdown|operate", it["cls"], re.I)]
        print("\n更多按钮候选 %d 个" % len(more))
        for m in more[:8]:
            print("   ", json.dumps(m, ensure_ascii=False))

        if not more:
            print("!! 没找到更多按钮,放弃")
            return

        tgt = more[0]
        c.start_collect()
        c.click(tgt["x"], tgt["y"])
        time.sleep(2.5)
        menu = json.loads(c.eval(JS_MENU) or "[]")
        c.drain()
        apis = api_requests(c.stop_collect())

        print("\n---- 点开后出现的短文本(菜单项)")
        for m in menu[:40]:
            print("   %-14s (%d,%d)" % (m["text"][:14], m["x"], m["y"]))
        print("\n---- 期间发生的请求 %d" % len(apis))
        for a in apis[:12]:
            print(f"   {a['method']:5s} {a['url'][:130]}")

        os.makedirs(OUT, exist_ok=True)
        json.dump({"items": items, "menu": menu, "apis": apis},
                  open(os.path.join(OUT, "work_menu.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存 work_menu.json")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
