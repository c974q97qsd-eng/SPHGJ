# -*- coding: utf-8 -*-
"""真实执行一次「回复评论」并抓包(修正版: 按 placeholder 定位回复框,在其祖先内找发送按钮)。

安全边界: 回复内容固定为 TEST_TEXT,回复对象是当前作品下第一条评论,后续可删。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/interactive/comment"
TEST_TEXT = sys.argv[1] if len(sys.argv) > 1 else "测试回复_接口定型_可删"

# 1) 找「回复」小按钮
JS_REPLY_BTN = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span').forEach(el => {
    if (!vis(el) || norm(el.innerText) !== '回复') return;
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               cls: String(el.className).slice(0, 50) });
  });
  return JSON.stringify(out);
})()
"""

# 2) 列出所有输入框(带 placeholder)
JS_INPUTS = r"""
(() => {
  const out = [];
  document.querySelectorAll('textarea,[contenteditable="true"],div[placeholder]').forEach(el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 40 || r.height < 8) return;
    if (st.display === 'none' || st.visibility === 'hidden') return;
    out.push({ tag: el.tagName, ph: el.getAttribute('placeholder') || '',
               cls: String(el.className).slice(0, 50),
               x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height) });
  });
  return JSON.stringify(out);
})()
"""

# 3) 在指定坐标的输入框所在祖先容器内找「发送」按钮
JS_SEND_NEAR = r"""
((boxX, boxY) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  // 找包含该坐标的最深元素
  let host = document.elementFromPoint(boxX, boxY);
  if (!host) return JSON.stringify([]);
  let node = host;
  for (let up = 0; up < 6 && node; up++) {
    const hits = [];
    node.querySelectorAll('div,span,button').forEach(el => {
      if (!vis(el) || norm(el.innerText) !== '发送') return;
      const r = el.getBoundingClientRect();
      hits.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                  tag: el.tagName, cls: String(el.className).slice(0, 50) });
    });
    if (hits.length) return JSON.stringify({ level: up, hits });
    node = node.parentElement;
  }
  return JSON.stringify({ level: -1, hits: [] });
})
"""


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=120)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待页面加载…")
        time.sleep(24)

        # 1) 点「回复」
        btns = json.loads(c.eval(JS_REPLY_BTN) or "[]")
        print("「回复」按钮:", json.dumps(btns, ensure_ascii=False)[:250])
        if not btns:
            print("!! 没有回复按钮"); return
        b = btns[0]
        c.start_collect()
        c.click(b["x"], b["y"])
        time.sleep(2.2)

        # 2) 选 placeholder 以「回复」开头的输入框
        inputs = json.loads(c.eval(JS_INPUTS) or "[]")
        print("输入框:", json.dumps(inputs, ensure_ascii=False)[:420])
        cand = [i for i in inputs if i["ph"].startswith("回复")]
        if not cand:
            print("!! 没有回复输入框"); return
        box = cand[0]
        print("选中回复框:", json.dumps(box, ensure_ascii=False))

        c.click(box["x"], box["y"])
        time.sleep(0.6)
        c.insert_text(TEST_TEXT)
        time.sleep(0.9)
        typed = c.eval(r"""(() => { const el = document.activeElement;
            if (!el) return '';
            return (el.value ? el.value : (el.innerText || '')); })()""")
        print("实际写入:", repr(typed)[:120])
        if TEST_TEXT not in (typed or ""):
            print("!! 注字失败"); return

        # 3) 在该输入框祖先里找发送按钮
        send = json.loads(c.eval(
            f"({JS_SEND_NEAR})({box['x']}, {box['y']})") or "{}")
        print("发送按钮:", json.dumps(send, ensure_ascii=False)[:400])
        hits = send.get("hits") or []
        if not hits:
            print("!! 没找到发送按钮"); return
        s = hits[0]
        c.click(s["x"], s["y"])
        time.sleep(4.5)
        c.drain()

        apis = api_requests(c.stop_collect())
        os.makedirs(OUT, exist_ok=True)
        json.dump({"testText": TEST_TEXT, "requests": apis},
                  open(os.path.join(OUT, "exec_reply.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        print("\n---- 业务请求 (%d)" % len(apis))
        for a in apis:
            print(f"  {a['method']:5s} {a['url'][:160]}")
            if a.get("postData"):
                print(f"        body: {a['postData'][:400]}")
            if a.get("headers"):
                print(f"        hdr : {json.dumps(a['headers'], ensure_ascii=False)[:220]}")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
