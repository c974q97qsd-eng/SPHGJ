# -*- coding: utf-8 -*-
"""实验: 纯 HTTP(不起浏览器)能否直接拿到 check_live_status 的响应。

只做可行性验证,不改任何东西:
  1. 从母本 config.json 读账号 storage_state 的 cookie
  2. 从母本 data/live_api_spec.json 读学到的 url/headers/postData
  3. 替换 timestamp 为当前毫秒,urllib 直发一次
  4. 打印状态码 + 响应前若干字 + 是否有 status/liveObjectId 字段

用法: python tools/probe_http_try.py [account_id]
"""
import io
import json
import sys
import time
import urllib.request
import urllib.error

BASE = r"\\Sphgj\sphgj"
ACC = sys.argv[1] if len(sys.argv) > 1 else "uspoloassn2"


def load_json(path):
    return json.load(io.open(path, encoding="utf-8"))


def main():
    cfg = load_json(BASE + r"\config.json")
    accs = cfg.get("accounts") or []
    acc = next((a for a in accs if a.get("id") == ACC), None)
    if not acc:
        print("找不到账号", ACC, "可用:", [a.get("id") for a in accs])
        return
    ss = acc.get("storage_state") or {}
    cookies = ss.get("cookies") or []
    if not cookies:
        print("该账号 storage_state 没有 cookie,换一个账号试")
        return

    spec_all = load_json(BASE + r"\data\live_api_spec.json")
    spec = (spec_all.get(ACC) or {}).get("live_status")
    if not spec:
        print("该账号没有 live_status 规格缓存")
        return

    # cookie 头: 只取 channels.weixin.qq.com 及其父域(.weixin.qq.com)
    jar = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        name, val = c.get("name"), c.get("value")
        if not name or val is None:
            continue
        if dom.endswith("weixin.qq.com"):
            jar.append("%s=%s" % (name, val))
    print("cookie 条数:", len(cookies), "-> 用于请求:", len(jar))

    headers = {k: v for k, v in (spec.get("headers") or {}).items()
               if k.lower() not in ("cookie", "content-length", "host")}
    headers["Cookie"] = "; ".join(jar)

    body = spec.get("postData") or ""
    now_ms = str(int(time.time() * 1000))
    # timestamp 必须新鲜(服务端可能校验),原值是学到那一刻的
    import re
    body2, n = re.subn(r'"timestamp"\s*:\s*"\d+"', '"timestamp":"%s"' % now_ms, body)
    print("timestamp 替换:", n, "处 ->", now_ms)

    url = spec["url"]
    method = (spec.get("method") or "POST").upper()
    print("请求:", method, url[:120])

    # 显式不走代理(母本可能没配代理;本机若有全局代理会干扰,先都试一遍)
    for label, opener in (
        ("直连", urllib.request.build_opener(urllib.request.ProxyHandler({}))),
        ("系统代理", urllib.request.build_opener()),
    ):
        req = urllib.request.Request(url, data=body2.encode("utf-8"),
                                     headers=headers, method=method)
        t0 = time.time()
        try:
            with opener.open(req, timeout=15) as r:
                txt = r.read().decode("utf-8", "replace")
                cost = (time.time() - t0) * 1000
                print("\n[%s] HTTP %s, %.0fms, %d 字节" % (label, r.status, cost, len(txt)))
                print(txt[:600])
                try:
                    d = json.loads(txt)
                    data = d.get("data") or {}
                    print("\n--- 解析 ---")
                    print("errCode:", d.get("errCode"), "errMsg:", d.get("errMsg"))
                    print("status:", data.get("status"),
                          "liveObjectId:", data.get("liveObjectId"))
                    print("audiencePlayUrl:", bool(data.get("audiencePlayUrl")),
                          "coverImgList:", len(data.get("coverImgList") or []))
                    print("data keys:", sorted(data.keys()))
                except Exception as e:
                    print("JSON 解析失败:", e)
                print("\n>>> %s 可行" % label)
                return
        except urllib.error.HTTPError as e:
            print("\n[%s] HTTPError %s: %s" % (label, e.code,
                  (e.read() or b"")[:300].decode("utf-8", "replace")))
        except Exception as e:
            print("\n[%s] 失败: %r" % (label, e))


if __name__ == "__main__":
    main()
