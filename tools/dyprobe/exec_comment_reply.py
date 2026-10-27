# -*- coding: utf-8 -*-
"""真实执行一次「回复评论」并抓包,用来定型 reply 接口的 URL/body/header。

安全边界: 回复内容固定为 TEST_TEXT,回复对象是当前作品下第一条评论。
回复本身可撤销(后续用 delete 接口删掉),不影响作品数据。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/interactive/comment"
TEST_TEXT = "测试回复_接口定型_可删"

# 找「回复」按钮的坐标
JS_FIND = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (t !== '%TEXT%') return;
    if (el.children.length > 2) return;
    const r = el.getBoundingClientRect();
    out.push({ text: t, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
               tag: el.tagName, cls: String(el.className).slice(0, 60) });
  });
  return JSON.stringify(out);
})()
"""

# 找输入框
JS_INPUT = r"""
(() => {
  const cands = [];
  document.querySelectorAll('textarea,input[type=text],[contenteditable="true"]').forEach(el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 20 || r.height < 8) return;
    if (st.display === 'none' || st.visibility === 'hidden') return;
    cands.push({ tag: el.tagName, cls: String(el.className).slice(0, 60),
                 x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
                 w: Math.round(r.width), h: Math.round(r.height),
                 ph: (el.getAttribute('placeholder') || '') });
  });
  return JSON.stringify(cands);
})()
"""


def find_text(c, text):
    raw = c.eval(JS_FIND.replace("%TEXT%", text))
    return json.loads(raw) if raw else []


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=120)
    ok = False
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待页面加载…")
        time.sleep(24)

        # 1) 点「回复」
        btns = find_text(c, "回复")
        print("「回复」按钮:", json.dumps(btns, ensure_ascii=False)[:300])
        if not btns:
            print("!! 没找到回复按钮")
            return
        b = btns[-1]
        c.start_collect()
        c.click(b["x"], b["y"])
        time.sleep(2.5)

        # 2) 找输入框并注字
        inputs = json.loads(c.eval(JS_INPUT) or "[]")
        print("输入框候选:", json.dumps(inputs, ensure_ascii=False)[:400])
        if not inputs:
            print("!! 没找到输入框")
            return
        box = sorted(inputs, key=lambda i: -(i["w"] * i["h"]))[0]
        c.click(box["x"], box["y"])
        time.sleep(0.6)
        c.insert_text(TEST_TEXT)
        time.sleep(0.8)
        typed = c.eval(r"""(() => {
            const el = document.activeElement;
            if (!el) return '';
            return (el.value !== undefined && el.value !== null && el.value !== '')
                ? el.value : (el.innerText || '');
        })()""")
        print("实际写入的文本:", repr(typed)[:120])
        if TEST_TEXT not in (typed or ""):
            print("!! 注字失败,放弃")
            return

        # 3) 点「发送」
        send = [x for x in find_text(c, "发送") if x["text"] == "发送"]
        print("「发送」按钮:", json.dumps(send, ensure_ascii=False)[:300])
        if not send:
            print("!! 没找到发送按钮")
            return
        s = send[-1]
        c.click(s["x"], s["y"])
        time.sleep(4)
        c.drain()

        reqs = c.stop_collect()
        apis = api_requests(reqs)
        os.makedirs(OUT, exist_ok=True)
        json.dump({"testText": TEST_TEXT, "allRequests": apis},
                  open(os.path.join(OUT, "exec_reply.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        print("\n---- 抓到的业务请求 (%d)" % len(apis))
        for a in apis:
            print(f"  {a['method']:5s} {a['url'][:150]}")
            if a.get("postData"):
                print(f"        body: {a['postData'][:300]}")
            if a.get("headers"):
                print(f"        hdr : {json.dumps(a['headers'], ensure_ascii=False)[:200]}")
        ok = True
    finally:
        c.close()
        close_tab(tid)
        print("\n完成:", ok)


if __name__ == "__main__":
    main()
