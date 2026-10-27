# -*- coding: utf-8 -*-
"""探评论管理页 DOM：找「...」操作菜单、回复框、以及当前页的评论项。

只读，不做任何点击(除了打开菜单这种无副作用操作可另行开关)。
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

URL = sys.argv[1] if len(sys.argv) > 1 else \
    "https://creator.douyin.com/creator-micro/data/following/comment"
WAIT = int(sys.argv[2]) if len(sys.argv) > 2 else 26

JS_DUMP = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  // 所有可见按钮/可点元素的文本
  const btns = [];
  document.querySelectorAll('button,[role="button"],span,div,a,i').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (!t || t.length > 14) return;
    const cls = (el.className && el.className.baseVal !== undefined)
        ? el.className.baseVal : (typeof el.className === 'string' ? el.className : '');
    if (!/btn|button|operate|action|icon|more|reply|delete|top/i.test(cls + ' ' + t)) return;
    btns.push({ tag: el.tagName, text: t, cls: String(cls).slice(0, 70) });
  });
  // 去重
  const seen = new Set(); const uniq = [];
  for (const b of btns) { const k = b.tag + '|' + b.text + '|' + b.cls; if (!seen.has(k)) { seen.add(k); uniq.push(b); } }

  // 页面正文前 1500 字，看布局
  const bodyText = norm(document.body.innerText).slice(0, 1500);

  // 找 iframe
  const frames = [];
  const walk = (w, d) => {
    if (!w || d > 4) return;
    try {
      w.document.querySelectorAll('iframe').forEach(f => {
        frames.push({ d, src: (f.src || '').slice(0, 120) });
        try { walk(f.contentWindow, d + 1); } catch (e) {}
      });
    } catch (e) {}
  };
  walk(window, 0);

  return JSON.stringify({ href: location.href, title: document.title,
                          bodyText, btns: uniq.slice(0, 60), frames });
})()
"""


def main():
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{URL}", method="PUT")
    tab = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=120)
    try:
        c.call("Runtime.enable")
        time.sleep(WAIT)
        raw = c.eval(JS_DUMP)
        d = json.loads(raw) if raw else {}
    finally:
        c.close()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=10).read()
        except Exception:
            pass

    os.makedirs(OUT, exist_ok=True)
    json.dump(d, open(os.path.join(OUT, "dom_comment.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print("URL:", d.get("href"))
    print("标题:", d.get("title"))
    print("\n---- frames")
    for f in d.get("frames") or []:
        print("   " + f["src"])
    print("\n---- 页面文本")
    print("   " + (d.get("bodyText") or "")[:900])
    print("\n---- 候选可点元素")
    for b in d.get("btns") or []:
        print("   %-6s %-12s %s" % (b["tag"], b["text"][:12], b["cls"]))


if __name__ == "__main__":
    main()
