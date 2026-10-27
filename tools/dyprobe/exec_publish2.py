# -*- coding: utf-8 -*-
"""发布(第2版): 精确定位发布按钮(primary button + 滚入视口), 并打印全部按钮便于诊断。

流程: 上传 -> 填描述 -> 设仅自己可见 -> 诊断按钮 -> 点发布 -> 等成功
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_captured")
URL = "https://creator.douyin.com/creator-micro/content/upload"
VIDEO = os.path.join(HERE, "_tmp", "upload_test.mp4")
TITLE = "接口测试_可删"

JS_STATE = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const t = norm(document.body.innerText);
  return JSON.stringify({ len: t.length, hasForm: /作品描述|谁可以看/.test(t),
                          uploading: /上传中|转码中|\\d+%/.test(t),
                          ok: /发布成功|发布完成/.test(t),
                          head: t.slice(0, 200) });
})()
"""

# 列出所有按钮
JS_BUTTONS = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  document.querySelectorAll('button,[role="button"]').forEach(el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const t = norm(el.innerText);
    if (!t) return;
    out.push({ t: t.slice(0, 16), cls: String(el.className).slice(0, 90),
               x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height),
               shown: r.width > 2 && r.height > 2 && st.display !== 'none' });
  });
  return JSON.stringify(out);
})()
"""

# 滚到发布按钮并返回它滚动后的坐标
JS_SCROLL_TO_PUB = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cands = [];
  document.querySelectorAll('button,[role="button"]').forEach(el => {
    const t = norm(el.innerText);
    if (t !== '发布') return;
    const cls = String(el.className);
    if (!/primary/i.test(cls)) return;   // 发布按钮是 primary 样式
    cands.push({ el, cls, y: el.getBoundingClientRect().top });
  });
  if (!cands.length) return JSON.stringify({ ok: false });
  cands.sort((a, b) => b.y - a.y);       // 页面下方的那个
  const el = cands[0].el;
  el.scrollIntoView({ block: 'center' });
  const r = el.getBoundingClientRect();
  return JSON.stringify({ ok: true, x: Math.round(r.left + r.width/2),
                          y: Math.round(r.top + r.height/2),
                          w: Math.round(r.width), h: Math.round(r.height),
                          cls: cands[0].cls.slice(0, 70),
                          disabled: el.disabled === true });
})()
"""

JS_EDITABLES = r"""
(() => {
  const out = [];
  document.querySelectorAll('[contenteditable="true"],textarea').forEach(el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    out.push({ ph: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
               x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height) });
  });
  out.sort((a, b) => b.w * b.h - a.w * a.h);
  return JSON.stringify(out);
})()
"""

JS_CLICK_INPUT = r"""
((label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cands = [];
  document.querySelectorAll('input[type=checkbox],input[type=radio]').forEach(el => {
    const r = el.getBoundingClientRect();
    let lab = ''; let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) { const t = norm(p.innerText); if (t) { lab = t; break; } p = p.parentElement; }
    cands.push({ el, lab, y: r.top, checked: el.checked });
  });
  const hit = cands.filter(c => c.lab.includes(label)).sort((a, b) => a.y - b.y)[0];
  if (!hit) return JSON.stringify({ ok: false, labs: cands.map(c => c.lab.slice(0, 16)) });
  const before = hit.checked;
  if (!before) hit.el.click();
  return JSON.stringify({ ok: true, before, after: hit.el.checked });
})
"""


def st(c):
    return json.loads(c.eval(JS_STATE) or "{}")


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=300)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        c.call("DOM.enable")
        print("等待发布页…")
        time.sleep(25)

        c.start_collect()
        oid = c.eval_obj("document.querySelector('input[type=file]')")
        if not oid:
            print("!! 无 file input"); return
        c.call("DOM.setFileInputFiles", files=[VIDEO], objectId=oid)
        print("已投喂视频…")

        for i in range(20):
            time.sleep(10)
            s = st(c)
            print("  [%3ds] form=%s uploading=%s" % ((i + 1) * 10, s.get("hasForm"), s.get("uploading")))
            if s.get("hasForm") and not s.get("uploading"):
                break
        time.sleep(6)

        # 填描述
        eds = json.loads(c.eval(JS_EDITABLES) or "[]")
        named = [e for e in eds if "描述" in e["ph"] or "标题" in e["ph"]]
        box = (named or eds or [None])[0]
        if box:
            c.click(box["x"], box["y"])
            time.sleep(0.5)
            c.insert_text(TITLE)
            time.sleep(1.0)
            print("描述:", repr(c.eval("(document.activeElement||{}).innerText||''"))[:60])

        # 私密
        print("权限:", c.eval(f"({JS_CLICK_INPUT})('仅自己可见')")[:180])
        time.sleep(1.0)

        # 诊断按钮
        btns = json.loads(c.eval(JS_BUTTONS) or "[]")
        os.makedirs(OUT, exist_ok=True)
        json.dump(btns, open(os.path.join(OUT, "pub_buttons.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n按钮清单(含'发布'的):")
        for b in btns:
            if "发布" in b["t"]:
                print("   %-10s primary=%s shown=%s (%d,%d) %s" % (
                    b["t"], "primary" in b["cls"], b["shown"], b["x"], b["y"], b["cls"][:60]))

        pos = json.loads(c.eval(JS_SCROLL_TO_PUB) or "{}")
        print("\n滚入视口后的发布按钮:", json.dumps(pos, ensure_ascii=False)[:260])
        if not pos.get("ok"):
            print("!! 没找到 primary 发布按钮"); return
        time.sleep(0.8)
        c.click(pos["x"], pos["y"])
        print("已点击发布…")

        for i in range(15):
            time.sleep(5)
            s = st(c)
            print("  [%2ds] ok=%s uploading=%s | %s" % ((i + 1) * 5, s.get("ok"),
                                                        s.get("uploading"), (s.get("head") or "")[:90]))
            if s.get("ok"):
                break
        time.sleep(6)
        c.drain()
        apis = api_requests(c.stop_collect(), needles=("://",))

        json.dump({"title": TITLE, "requests": apis, "state": st(c)},
                  open(os.path.join(OUT, "publish_submit2.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        print("\n---- 提交类请求")
        for a in apis:
            u = a["url"]
            if not any(k in u for k in ("create_v2", "aweme/create", "publish/commit", "media/aweme")):
                continue
            if "limit_app_groups" in u:
                continue
            print(f"  {a['method']:5s} {u[:180]}")
            if a.get("postData"):
                print("        body:", a["postData"][:1800])
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
