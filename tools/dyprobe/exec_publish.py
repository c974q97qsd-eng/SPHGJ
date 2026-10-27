# -*- coding: utf-8 -*-
"""完整走一次发布(私密): 上传 -> 填描述 -> 设仅自己可见 -> 点发布, 抓 create_v2。

安全: 权限先设为「仅自己可见」,发布后立刻记录 aweme_id 便于删除。
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
                          hasUploading: /上传中|处理中|转码中/.test(t),
                          head: t.slice(0, 260) });
})()
"""

JS_EDITABLES = r"""
(() => {
  const out = [];
  document.querySelectorAll('[contenteditable="true"],textarea').forEach(el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    out.push({ ph: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
               x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               w: Math.round(r.width), h: Math.round(r.height), tag: el.tagName });
  });
  out.sort((a, b) => b.w * b.h - a.w * a.h);
  return JSON.stringify(out);
})()
"""

# 找可点文本(精确),排除指定词
JS_FIND = r"""
((args) => {
  const text = args.text, exclude = args.exclude || [];
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,button,label').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (t !== text) return;
    if (exclude.some(x => t.includes(x))) return;
    for (const ch of el.children) { if (norm(ch.innerText) === text) return; }
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 40) });
  });
  out.sort((a, b) => b.y - a.y);   // 发布按钮通常在页面下方
  return JSON.stringify(out);
})
"""

# JS 直接 click 指定 label 的 checkbox/radio
JS_CLICK_INPUT = r"""
((args) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cands = [];
  document.querySelectorAll('input[type=checkbox],input[type=radio]').forEach(el => {
    const r = el.getBoundingClientRect();
    let lab = ''; let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++) { const t = norm(p.innerText); if (t) { lab = t; break; } p = p.parentElement; }
    cands.push({ el, lab, y: r.top, checked: el.checked });
  });
  const hit = cands.filter(c => c.lab.includes(args.label)).sort((a, b) => a.y - b.y)[0];
  if (!hit) return JSON.stringify({ ok: false, labs: cands.map(c => c.lab.slice(0, 16)) });
  const before = hit.checked;
  if (!before) hit.el.click();
  return JSON.stringify({ ok: true, before, after: hit.el.checked, lab: hit.lab.slice(0, 20) });
})
"""


def state(c):
    return json.loads(c.eval(JS_STATE) or "{}")


def find(c, text, exclude=()):
    args = json.dumps({"text": text, "exclude": list(exclude)}, ensure_ascii=False)
    return json.loads(c.eval(f"({JS_FIND})({args})") or "[]")


def main():
    if not os.path.exists(VIDEO):
        print("!! 缺视频"); return
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
            print("!! 无 file input", json.dumps(state(c), ensure_ascii=False)[:200]); return
        c.call("DOM.setFileInputFiles", files=[VIDEO], objectId=oid)
        print("已投喂视频,等待转码…")

        for i in range(18):
            time.sleep(10)
            st = state(c)
            print("  [%3ds] form=%s uploading=%s" % ((i + 1) * 10, st.get("hasForm"), st.get("hasUploading")))
            if st.get("hasForm") and not st.get("hasUploading"):
                break
        time.sleep(5)

        # ---- 填描述
        eds = json.loads(c.eval(JS_EDITABLES) or "[]")
        print("可编辑框:", json.dumps(eds, ensure_ascii=False)[:350])
        named = [e for e in eds if "描述" in e["ph"] or "标题" in e["ph"]]
        box = (named or eds or [None])[0]
        if box:
            c.click(box["x"], box["y"])
            time.sleep(0.6)
            c.insert_text(TITLE)
            time.sleep(1.0)
            print("描述已填:", repr(c.eval(r"""(() => { const el = document.activeElement;
                return el ? (el.innerText || el.value || '') : ''; })()"""))[:80])
        else:
            print("!! 没有可编辑框")

        # ---- 设仅自己可见
        r = json.loads(c.eval(f"({JS_CLICK_INPUT})({json.dumps({'label': '仅自己可见'}, ensure_ascii=False)})") or "{}")
        print("权限设置:", json.dumps(r, ensure_ascii=False)[:220])

        # ---- 点发布(排除 定时发布/发布暂存离开/作品发布)
        pubs = find(c, "发布", exclude=("定时发布", "暂存", "作品发布", "同时发布"))
        print("「发布」按钮候选:", json.dumps(pubs, ensure_ascii=False)[:350])
        pubs = [p for p in pubs if p["tag"] in ("BUTTON", "DIV", "SPAN")]
        if not pubs:
            print("!! 没找到发布按钮"); return
        target = pubs[-1]
        print("点击发布 @", target["x"], target["y"])
        c.click(target["x"], target["y"])

        # ---- 等发布结果
        for i in range(12):
            time.sleep(5)
            st = state(c)
            head = (st.get("head") or "")
            print("  [%2ds] %s" % ((i + 1) * 5, head[:120]))
            if "发布成功" in head or "作品管理" in head or "审核" in head:
                break
        time.sleep(5)
        c.drain()
        apis = api_requests(c.stop_collect(), needles=("://",))

        os.makedirs(OUT, exist_ok=True)
        json.dump({"title": TITLE, "requests": apis, "state": state(c)},
                  open(os.path.join(OUT, "publish_submit.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        # ---- 重点找 create / publish 提交请求
        print("\n---- 提交类请求")
        for a in apis:
            u = a["url"]
            if not any(k in u for k in ("create_v2", "aweme/create", "publish", "media/aweme")):
                continue
            print(f"  {a['method']:5s} {u[:180]}")
            if a.get("postData"):
                print("        body:", a["postData"][:1200])

        print("\n---- 全部业务请求(去重)")
        seen = set()
        for a in apis:
            u = a["url"]
            if any(x in u for x in ("blob:", ".js", ".css", ".ttf", ".png", ".jpg", "tos-",
                                    "mssdk", "mcs.snssdk", "zijieapi", "mon.", "font")):
                continue
            k = a["method"] + u.split("?")[0]
            if k in seen:
                continue
            seen.add(k)
            print("  " + k[:150])
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
