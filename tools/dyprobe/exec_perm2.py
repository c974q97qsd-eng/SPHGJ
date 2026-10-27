# -*- coding: utf-8 -*-
"""抓作品权限接口(用 JS 直接 click input,坐标点击不触发 React onChange)。

流程: 点「设置权限」-> JS click「仅自己可见」的 input -> 抓包 -> JS click「公开」的 input
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
  document.querySelectorAll('div,span,label').forEach(el => {
    if (!vis(el) || norm(el.innerText) !== text) return;
    for (const ch of el.children) { if (norm(ch.innerText) === text) return; }
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) });
  });
  out.sort((a, b) => a.y - b.y || a.x - b.x);
  return JSON.stringify(out);
})
"""

# 直接 click 指定 label 对应的 input(取 y 最小的那一行)
JS_CLICK_INPUT = r"""
((labelText) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cands = [];
  document.querySelectorAll('input[type=checkbox],input[type=radio]').forEach(el => {
    const r = el.getBoundingClientRect();
    let lab = '';
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) {
      const t = norm(p.innerText);
      if (t) { lab = t; break; }
      p = p.parentElement;
    }
    cands.push({ el, lab, y: r.top, x: r.left, checked: el.checked });
  });
  // label 文本里包含目标词的,取 y 最小
  const hit = cands.filter(c => c.lab.includes(labelText)).sort((a, b) => a.y - b.y)[0];
  if (!hit) return JSON.stringify({ ok: false, all: cands.map(c => ({ lab: c.lab.slice(0, 18), y: Math.round(c.y), checked: c.checked })) });
  const before = hit.checked;
  hit.el.click();
  return JSON.stringify({ ok: true, before, lab: hit.lab.slice(0, 24),
                          y: Math.round(hit.y), x: Math.round(hit.x) });
})
"""

JS_STATE = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const res = [];
  document.querySelectorAll('input[type=checkbox],input[type=radio]').forEach(el => {
    const r = el.getBoundingClientRect();
    let lab = ''; let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) { const t = norm(p.innerText); if (t) { lab = t; break; } p = p.parentElement; }
    res.push({ checked: el.checked, y: Math.round(r.top), lab: lab.slice(0, 20) });
  });
  return JSON.stringify(res.slice(0, 10));
})()
"""


def find(c, text):
    return json.loads(c.eval(f"({JS_FIND})({json.dumps(text, ensure_ascii=False)})") or "[]")


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=150)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待页面加载…")
        time.sleep(26)

        setting = find(c, "设置权限")
        print("「设置权限」:", json.dumps(setting, ensure_ascii=False)[:160])
        if not setting:
            print("!! 没找到设置权限"); return
        c.click(setting[0]["x"], setting[0]["y"])
        time.sleep(2.0)

        # ---- 1) 仅自己可见
        c.start_collect()
        r1 = json.loads(c.eval(f"({JS_CLICK_INPUT})('仅自己可见')") or "{}")
        print("点「仅自己可见」:", json.dumps(r1, ensure_ascii=False)[:250])
        time.sleep(6.0)
        c.drain()
        a1 = api_requests(c.stop_collect(), needles=("://",))
        print("改私密后:", json.dumps(json.loads(c.eval(JS_STATE) or "[]"), ensure_ascii=False)[:260])

        # ---- 2) 改回公开
        c.start_collect()
        r2 = json.loads(c.eval(f"({JS_CLICK_INPUT})('公开')") or "{}")
        print("点「公开」:", json.dumps(r2, ensure_ascii=False)[:250])
        time.sleep(6.0)
        c.drain()
        a2 = api_requests(c.stop_collect(), needles=("://",))
        print("改回后:", json.dumps(json.loads(c.eval(JS_STATE) or "[]"), ensure_ascii=False)[:260])

        def show(tag, reqs):
            print("\n---- %s 请求 %d" % (tag, len(reqs)))
            for a in reqs:
                u = a["url"]
                if any(x in u for x in (".css", ".js", ".png", ".jpg", ".woff",
                                        "check/user", "work_list", "user_message/notice",
                                        "mon.zijieapi", "mssdk.bytedance", "log.snssdk")):
                    continue
                print(f"  {a['method']:5s} {a['url'][:170]}")
                if a.get("postData"):
                    print("        body:", a["postData"][:400])
                if a.get("headers"):
                    print("        hdr :", json.dumps(a["headers"], ensure_ascii=False)[:220])

        show("改私密", a1)
        show("改回公开", a2)

        os.makedirs(OUT, exist_ok=True)
        json.dump({"toPrivate": a1, "toPublic": a2},
                  open(os.path.join(OUT, "perm_write2.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存 perm_write2.json")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
