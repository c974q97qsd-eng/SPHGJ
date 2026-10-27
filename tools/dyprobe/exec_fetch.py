# -*- coding: utf-8 -*-
"""页面内 API 调用器: 在已登录页面的同源上下文里 fetch,自动带 cookie 与 csrf。

用途:
  1) 用接口直接验证数据(比 UI 交互可靠)
  2) 探测写接口路径(未知路径会返回明确错误,不会误伤)
  3) 后续工程里也可直接复用这个思路(先浏览器内 fetch,再考虑纯 HTTP)

用法:
  python exec_fetch.py <页面URL> <json_file>
  json_file 内容: [{"name":"...","method":"GET","path":"/aweme/v1/...","body":{...}}, ...]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, close_tab, open_tab  # noqa: E402

JS_FETCH = r"""
(async (calls) => {
  const out = [];
  for (const c of calls) {
    const go = async (method, url, body, ct) => {
      const isForm = ct === 'form';
      const opt = { method, credentials: 'include',
                    headers: isForm
                      ? { 'content-type': 'application/x-www-form-urlencoded' }
                      : { 'content-type': 'application/json' } };
      if (body !== null && body !== undefined) {
        opt.body = isForm
          ? Object.keys(body).map(k => encodeURIComponent(k) + '=' + encodeURIComponent(body[k])).join('&')
          : JSON.stringify(body);
      }
      const t0 = performance.now();
      try {
        const r = await fetch(url, opt);
        const txt = await r.text();
        return { status: r.status, ms: Math.round(performance.now() - t0), body: txt.slice(0, 400000) };
      } catch (e) {
        return { status: -1, ms: Math.round(performance.now() - t0), body: 'ERR ' + String(e) };
      }
    };
    // 自动补 aid / msToken 不是必须的,先按原样发
    const res = await go(c.method || 'GET', c.path, c.body, c.ct);
    out.push({ name: c.name, path: c.path, ...res });
  }
  return JSON.stringify(out);
})
"""


def main():
    url = sys.argv[1]
    spec_path = sys.argv[2]
    calls = json.load(open(spec_path, encoding="utf-8"))

    tab = open_tab(url)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=180)
    try:
        c.call("Runtime.enable")
        print("等待页面就绪…")
        time.sleep(22)
        raw = c.eval(f"({JS_FETCH})({json.dumps(calls, ensure_ascii=False)})")
        results = json.loads(raw) if raw else []
    finally:
        c.close()
        close_tab(tid)

    for r in results:
        print("\n===== %s  [%s] %s" % (r["name"], r["status"], r["path"][:110]))
        print("   耗时 %sms" % r["ms"])
        print("   " + (r["body"] or "")[:1200].replace("\n", " "))

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_captured",
                       "exec_fetch_%s.json" % time.strftime("%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n已存:", out)


if __name__ == "__main__":
    main()
