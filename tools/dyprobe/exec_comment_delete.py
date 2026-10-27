# -*- coding: utf-8 -*-
"""删除上一步刚发出的测试回复,抓 comment/delete 接口。

若该评论不在当前页,先点「查看N条回复」展开。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/interactive/comment"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "测试回复_接口定型_可删"

# 1) 先展开回复列表(如果有「查看N条回复」)
JS_EXPAND = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (!/^查看\d+条回复$/.test(t)) return;
    const r = el.getBoundingClientRect();
    out.push({ t, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2) });
  });
  return JSON.stringify(out);
})()
"""

# 2) 找包含目标文本的评论块,在其祖先里找「删除」
JS_FIND_DELETE = r"""
((target) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  // 最内层包含 target 的元素
  let hits = [];
  document.querySelectorAll('div,span,p').forEach(el => {
    if (!vis(el) || !norm(el.innerText).includes(target)) return;
    // 只要最内层(子元素里不含 target)
    for (const ch of el.children) { if (norm(ch.innerText).includes(target)) return; }
    hits.push(el);
  });
  if (!hits.length) return JSON.stringify({ found: false });
  const box = hits[hits.length - 1];
  const br = box.getBoundingClientRect();
  const out = { found: true, textBox: { x: Math.round(br.left+br.width/2), y: Math.round(br.top+br.height/2) },
                levels: [] };
  let node = box;
  for (let up = 0; up < 8 && node; up++) {
    const dels = [];
    node.querySelectorAll('div,span,button').forEach(el => {
      if (!vis(el) || norm(el.innerText) !== '删除') return;
      for (const ch of el.children) { if (norm(ch.innerText) === '删除') return; }
      const r = el.getBoundingClientRect();
      dels.push({ x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2),
                  tag: el.tagName, cls: String(el.className).slice(0, 44) });
    });
    if (dels.length) { out.levels.push({ level: up, dels }); out.hitLevel = up; out.dels = dels; break; }
    node = node.parentElement;
  }
  return JSON.stringify(out);
})
"""

# 3) 找确认弹窗里的按钮
JS_CONFIRM = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('button,div,span').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (!['确定','确认','删除','取消','确定删除'].includes(t)) return;
    for (const ch of el.children) { if (norm(ch.innerText) === t) return; }
    const r = el.getBoundingClientRect();
    out.push({ t, x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 44) });
  });
  return JSON.stringify(out);
})()
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

        # 展开回复
        exp = json.loads(c.eval(JS_EXPAND) or "[]")
        print("展开按钮:", json.dumps(exp, ensure_ascii=False)[:250])
        if exp:
            c.click(exp[0]["x"], exp[0]["y"])
            time.sleep(3)

        # 找目标评论 + 删除按钮
        d = json.loads(c.eval(f"({JS_FIND_DELETE})({json.dumps(TARGET, ensure_ascii=False)})") or "{}")
        print("定位:", json.dumps(d, ensure_ascii=False)[:400])
        if not d.get("found"):
            print("!! 页面上没找到目标评论,可能已被清掉")
            return
        dels = d.get("dels") or []
        if not dels:
            print("!! 该评论附近没有删除按钮")
            return
        dl = dels[0]
        c.start_collect()
        c.click(dl["x"], dl["y"])
        time.sleep(2.0)

        # 可能有二次确认
        conf = json.loads(c.eval(JS_CONFIRM) or "[]")
        print("确认弹窗按钮:", json.dumps(conf, ensure_ascii=False)[:350])
        pick = None
        for it in conf:
            if it["t"] in ("确定", "确认", "删除", "确定删除"):
                pick = it
                break
        if pick:
            print("点确认:", pick["t"])
            c.click(pick["x"], pick["y"])
            time.sleep(3.5)
        else:
            print("(无二次确认弹窗)")
        c.drain()

        apis = api_requests(c.stop_collect())
        os.makedirs(OUT, exist_ok=True)
        json.dump({"target": TARGET, "requests": apis},
                  open(os.path.join(OUT, "exec_delete.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n---- 业务请求 (%d)" % len(apis))
        for a in apis:
            print(f"  {a['method']:5s} {a['url'][:160]}")
            if a.get("postData"):
                print(f"        body: {a['postData'][:400]}")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
