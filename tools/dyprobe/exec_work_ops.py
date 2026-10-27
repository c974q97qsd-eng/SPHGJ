# -*- coding: utf-8 -*-
"""在作品管理页点一个指定文本的操作按钮并抓包。

用法: python exec_work_ops.py "取消置顶"
安全: 只点文本精确匹配的元素; 拒绝点「删除作品」这类不可逆操作(列表见 BLOCK)。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, api_requests, close_tab, open_tab  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured")
URL = "https://creator.douyin.com/creator-micro/content/manage"

BLOCK = ("删除作品", "删除", "退出账号", "切换账号")

JS_FIND = r"""
((text) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('div,span,button,a').forEach(el => {
    if (!vis(el)) return;
    if (norm(el.innerText) !== text) return;
    for (const ch of el.children) { if (norm(ch.innerText) === text) return; }
    const r = el.getBoundingClientRect();
    out.push({ x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 60) });
  });
  return JSON.stringify(out);
})
"""

# 若有二次确认弹窗,列出按钮
JS_CONFIRM = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = [];
  document.querySelectorAll('button,div,span').forEach(el => {
    if (!vis(el)) return;
    const t = norm(el.innerText);
    if (!['确定','确认','取消','好','我知道了','确定取消'].includes(t)) return;
    for (const ch of el.children) { if (norm(ch.innerText) === t) return; }
    const r = el.getBoundingClientRect();
    out.push({ t, x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2),
               tag: el.tagName, cls: String(el.className).slice(0, 44) });
  });
  return JSON.stringify(out);
})()
"""

# 页面文本(确认操作后的状态变化)
JS_STATE = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const tags = [];
  document.querySelectorAll('div,span').forEach(el => {
    const t = norm(el.innerText);
    if (!t || t.length > 12) return;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 8) return;
    if (/置顶|权限|公开|私密|好友/.test(t)) tags.push({ t, x: Math.round(r.left), y: Math.round(r.top) });
  });
  return JSON.stringify(tags.slice(0, 40));
})()
"""


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "取消置顶"
    if text in BLOCK:
        print("!! 拒绝执行不可逆操作:", text)
        return

    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=150)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        print("等待作品管理页加载…")
        time.sleep(26)

        hits = json.loads(c.eval(f"({JS_FIND})({json.dumps(text, ensure_ascii=False)})") or "[]")
        print(f"「{text}」命中 {len(hits)} 个:", json.dumps(hits, ensure_ascii=False)[:300])
        if not hits:
            print("!! 没找到按钮")
            return

        tgt = hits[0]
        c.start_collect()
        c.click(tgt["x"], tgt["y"])
        time.sleep(2.5)

        conf = json.loads(c.eval(JS_CONFIRM) or "[]")
        print("确认弹窗:", json.dumps(conf, ensure_ascii=False)[:300])
        if conf:
            ok = [x for x in conf if x["t"] in ("确定", "确认", "好", "我知道了", "确定取消")]
            if ok:
                print("点确认:", ok[0]["t"])
                c.click(ok[0]["x"], ok[0]["y"])
                time.sleep(3.0)
        c.drain()

        apis = api_requests(c.stop_collect())
        state = json.loads(c.eval(JS_STATE) or "[]")

        print("\n---- 请求 (%d)" % len(apis))
        for a in apis:
            print(f"  {a['method']:5s} {a['url'][:160]}")
            if a.get("postData"):
                print(f"        body: {a['postData'][:400]}")
        print("\n---- 页面状态标签")
        for s in state[:20]:
            print("   %-14s (%d,%d)" % (s["t"], s["x"], s["y"]))

        os.makedirs(OUT, exist_ok=True)
        name = "work_%s.json" % ("untop" if "取消" in text else "top")
        json.dump({"action": text, "requests": apis, "state": state},
                  open(os.path.join(OUT, name), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存", name)
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
