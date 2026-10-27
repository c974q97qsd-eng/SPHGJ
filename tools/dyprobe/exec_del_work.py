# -*- coding: utf-8 -*-
"""按描述文本定位作品行,点它的「删除作品」并确认,抓 delete 接口。

安全: 先精确定位含目标描述的最内层元素,再在其**祖先容器内**找「删除作品」,
保证不会误点其他作品的按钮。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/content/manage"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "接口测试_可删"

JS_LOCATE = r"""
((target) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  // 最内层包含 target 的元素
  const hits = [];
  document.querySelectorAll('div,span,p').forEach(el => {
    if (!vis(el) || !norm(el.innerText).includes(target)) return;
    for (const ch of el.children) { if (norm(ch.innerText).includes(target)) return; }
    hits.push(el);
  });
  if (!hits.length) return JSON.stringify({ found: false });
  const box = hits[hits.length - 1];
  const br = box.getBoundingClientRect();
  // 向上找带「删除作品」的祖先
  let node = box, res = null;
  for (let up = 0; up < 10 && node; up++) {
    const dels = [];
    node.querySelectorAll('div,span,button').forEach(el => {
      if (!vis(el) || norm(el.innerText) !== '删除作品') return;
      for (const ch of el.children) { if (norm(ch.innerText) === '删除作品') return; }
      const r = el.getBoundingClientRect();
      dels.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
                  cls: String(el.className).slice(0, 40) });
    });
    if (dels.length) { res = { level: up, dels }; break; }
    node = node.parentElement;
  }
  return JSON.stringify({ found: true, textY: Math.round(br.top),
                          textX: Math.round(br.left), btn: res });
})
"""

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
               tag: el.tagName });
  });
  return JSON.stringify(out);
})()
"""


def main():
    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=180)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待作品管理页…")
        time.sleep(26)

        loc = json.loads(c.eval(f"({JS_LOCATE})({json.dumps(TARGET, ensure_ascii=False)})") or "{}")
        print("定位:", json.dumps(loc, ensure_ascii=False)[:350])
        if not loc.get("found"):
            print("!! 页面上没找到目标作品(可能在后面几页)"); return
        btn = (loc.get("btn") or {}).get("dels") or []
        if not btn:
            print("!! 该作品行内没有删除按钮(可能需要先 hover)")
            # 直接关掉, 不乱点
            return

        b = btn[0]
        print("点击删除作品 @", b["x"], b["y"], b["cls"])
        c.start_collect()
        c.click(b["x"], b["y"])
        time.sleep(2.2)

        conf = json.loads(c.eval(JS_CONFIRM) or "[]")
        print("确认弹窗:", json.dumps(conf, ensure_ascii=False)[:320])
        picked = None
        for it in conf:
            if it["t"] in ("确定", "确认", "删除", "确定删除"):
                picked = it
                break
        if picked:
            print("点确认:", picked["t"])
            c.click(picked["x"], picked["y"])
            time.sleep(4.0)
        c.drain()
        apis = api_requests(c.stop_collect(), needles=("://",))

        os.makedirs(OUT, exist_ok=True)
        json.dump({"target": TARGET, "requests": apis},
                  open(os.path.join(OUT, "work_delete.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n---- 请求")
        for a in apis:
            u = a["url"]
            if any(x in u for x in (".js", ".css", "blob:", "mssdk", "mcs.snssdk",
                                    "zijieapi", "mon.", "work_list", "check/user", "notice")):
                continue
            print(f"  {a['method']:5s} {u[:170]}")
            if a.get("postData"):
                print("        body:", a["postData"][:500])
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
