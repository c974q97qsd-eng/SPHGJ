# -*- coding: utf-8 -*-
"""抓发布链路: 上传视频到发布页,停在编辑态(不点发布)。

抓到: upload/config、upload/auth/v5、TOS 直传请求(分片/单次)、以及编辑页所需的字段。
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

JS_TEXT = r"""
(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const t = norm(document.body.innerText);
  return JSON.stringify({
    len: t.length, head: t.slice(0, 700),
    hasUploading: /上传中|处理中|转码中|\d+%/.test(t),
    hasForm: /作品描述|标题|添加话题|发布时间|谁可以看/.test(t),
    fileInputs: document.querySelectorAll('input[type=file]').length,
  });
})()
"""


def main():
    if not os.path.exists(VIDEO):
        print("!! 缺测试视频", VIDEO)
        return

    tab = open_tab(URL)
    tid = tab["id"]
    c = CDP(tab["webSocketDebuggerUrl"], timeout=300)
    try:
        c.call("Runtime.enable")
        c.call("Network.enable")
        c.call("DOM.enable")
        print("等待发布页加载…")
        time.sleep(25)

        st = json.loads(c.eval(JS_TEXT) or "{}")
        print("页面状态:", json.dumps(st, ensure_ascii=False)[:400])
        if not st.get("fileInputs"):
            print("!! 没有 file input(可能被隐藏,或页面需先点上传)")
            # 试着找「上传视频」按钮
            r = c.eval(r"""(() => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const out = [];
              document.querySelectorAll('div,span,button').forEach(el => {
                const t = norm(el.innerText);
                if (!t || t.length > 10) return;
                if (!/上传|点击|选择/.test(t)) return;
                const r0 = el.getBoundingClientRect();
                if (r0.width < 10) return;
                out.push({ t, x: Math.round(r0.left + r0.width/2), y: Math.round(r0.top + r0.height/2) });
              });
              return JSON.stringify(out.slice(0, 10));
            })()""")
            print("上传入口候选:", r[:300])
            return

        # 设置文件
        c.start_collect()
        oid = c.eval_obj("document.querySelector('input[type=file]')")
        print("file input objectId:", (oid or "")[:40])
        if not oid:
            print("!! 取不到 file input 句柄")
            return
        c.call("DOM.setFileInputFiles", files=[VIDEO], objectId=oid)
        print("已投喂文件,等待上传/转码…")

        # 轮询状态,最多 150s
        for i in range(15):
            time.sleep(10)
            st = json.loads(c.eval(JS_TEXT) or "{}")
            print("  [%3ds] %s" % ((i + 1) * 10, json.dumps(st, ensure_ascii=False)[:260]))
            if st.get("hasForm") and not st.get("hasUploading"):
                print("  -> 已进入编辑态")
                break
        time.sleep(4)
        c.drain()
        reqs = c.stop_collect()
        apis = api_requests(reqs, needles=("://",))

        # 分类展示
        skip = (".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".svg",
                "mssdk.bytedance", "mcs.snssdk", "zijieapi", "log.snssdk",
                "check/user", "user_message/notice", "mon.", "/list")
        print("\n---- 请求 (%d 总, %d 已过滤展示)" % (len(apis), len(apis)))
        for a in apis:
            u = a["url"]
            if any(x in u for x in skip):
                continue
            print(f"  {a['method']:5s} {u[:160]}")
            if a.get("postData"):
                print("        body:", a["postData"][:300])

        os.makedirs(OUT, exist_ok=True)
        json.dump({"requests": apis, "state": st},
                  open(os.path.join(OUT, "publish_upload.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("\n已存 publish_upload.json")
    finally:
        c.close()
        close_tab(tid)


if __name__ == "__main__":
    main()
