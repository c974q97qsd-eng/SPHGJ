# -*- coding: utf-8 -*-
"""抓作品权限设置接口: 点「仅自己可见」-> 抓包 -> 立刻点回「公开」。

风险控制: 同一会话内连续操作,权限改动只存在 1~2 秒;若改回失败会明确报错。
只作用于列表第一个作品。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/content/manage"

JS_FIND_TOP = r"""
((text) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,label').forEach(el => {
    if (!vis(el) || norm(el.innerText) !== text) return;
    for (const ch of el.children) { if (norm(ch.innerText) === text) return; }
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 50) });
  });
  out.sort((a, b) => a.y - b.y || a.x - b.x);   // 第一行优先
  return JSON.stringify(out);
})
"""

JS_CHECKED = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const res = [];
  document.querySelectorAll('input[type=checkbox]').forEach(el => {
    const r = el.getBoundingClientRect();
    let lab = '';
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++) { const t = norm(p.innerText); if (t) { lab = t.slice(0, 20); break; } p = p.parentElement; }
    res.push({ checked: el.checked, y: Math.round(r.top), x: Math.round(r.left), lab });
  });
  return JSON.stringify(res.slice(0, 12));
})()
"""


def find(c, text):
    return json.loads(c.eval(f"({JS_FIND_TOP})({json.dumps(text, ensure_ascii=False)})") or "[]")


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=150)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待页面加载…")
        time.sleep(26)

        # 权限选项要先点「设置权限」才显示
        setting = find(c, "设置权限")
        print("「设置权限」命中:", json.dumps(setting, ensure_ascii=False)[:200])
        if setting:
            c.click(setting[0]["x"], setting[0]["y"])
            time.sleep(2.0)
            print("点开后 checkbox:", json.dumps(json.loads(c.eval(JS_CHECKED) or "[]"),
                                             ensure_ascii=False)[:300])

        # ---- 1) 改成「仅自己可见」
        priv = find(c, "仅自己可见")
        print("「仅自己可见」命中:", json.dumps(priv, ensure_ascii=False)[:200])
        if not priv:
            print("!! 没找到"); return
        c.start_collect()
        c.click(priv[0]["x"], priv[0]["y"])
        time.sleep(2.2)
        c.drain()
        a1 = api_requests(c.stop_collect())

        # ---- 2) 立刻改回「公开」
        pub = find(c, "公开")
        print("「公开」命中:", json.dumps(pub, ensure_ascii=False)[:200])
        a2 = []
        if pub:
            c.start_collect()
            c.click(pub[0]["x"], pub[0]["y"])
            time.sleep(2.2)
            c.drain()
            a2 = api_requests(c.stop_collect())
        time.sleep(2)
        print("最终 checkbox:", json.dumps(json.loads(c.eval(JS_CHECKED) or "[]"),
                                       ensure_ascii=False)[:300])

        def show(tag, reqs):
            print("\n---- %s 请求 %d" % (tag, len(reqs)))
            for a in reqs:
                if "check/user" in a["url"] or "work_list" in a["url"]:
                    continue
                print(f"  {a['method']:5s} {a['url'][:170]}")
                if a.get("postData"):
                    print("        body:", a["postData"][:400])
                if a.get("headers"):
                    print("        hdr :", json.dumps(a["headers"], ensure_ascii=False)[:200])

        show("改私密", a1)
        show("改回公开", a2)

        os.makedirs(OUT, exist_ok=True)
        json.dump({"toPrivate": a1, "toPublic": a2},
                  open(os.path.join(OUT, "perm_write.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存 perm_write.json")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
