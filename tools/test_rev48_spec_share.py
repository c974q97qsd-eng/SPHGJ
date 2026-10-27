# -*- coding: utf-8 -*-
"""rev48 实验: 没学过接口规格的账号,能否借用别人的规格(换成自己的账号标识)。

背景:live_api_spec.json 是"跑过直播大屏页才学到"的,只有 2/4/8 号有。
其余账号没规格 -> HTTP 探测用不上 -> 只能起浏览器现学。但规格其实只有三处是
账号相关的:URL 的 `_aid`、body 的 `_log_finder_id`、以及 cookie。

本脚本验证:拿 2 号的规格当模板,把上面三样换成目标账号自己的,
返回的 `wxNickname` 是不是目标账号本人(而不是 2 号)。
"""
import io
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = r"\\Sphgj\sphgj"
TEMPLATE_ACC = "uspoloassn2"


def load(path):
    return json.load(io.open(path, encoding="utf-8"))


def main():
    cfg = load(os.path.join(BASE, "config.json"))
    accs = {a["id"]: a for a in (cfg.get("accounts") or [])}
    specs = load(os.path.join(BASE, "data", "live_api_spec.json"))
    tpl = (specs.get(TEMPLATE_ACC) or {}).get("live_status")
    if not tpl:
        print("模板账号没有规格缓存,实验无法进行")
        return
    print("模板:", TEMPLATE_ACC, "url 长度", len(tpl["url"]))
    print()

    targets = [a for a in accs if a != TEMPLATE_ACC]
    for aid in targets:
        acc = accs[aid]
        jar = "; ".join("%s=%s" % (c["name"], c["value"])
                        for c in ((acc.get("storage_state") or {}).get("cookies") or [])
                        if (c.get("domain") or "") == "channels.weixin.qq.com")
        if not jar:
            print("%-14s 跳过(无 channels 域 cookie)" % aid)
            continue
        my_aid = acc.get("_aid") or ""
        my_fid = acc.get("_log_finder_id") or ""
        url = tpl["url"]
        if my_aid:
            url = re.sub(r"_aid=[^&]+", "_aid=" + my_aid, url, count=1)
        body = tpl.get("postData") or ""
        body = re.sub(r'"timestamp"\s*:\s*"\d+"',
                      '"timestamp":"%d"' % int(time.time() * 1000), body, count=1)
        if my_fid:
            body = re.sub(r'"_log_finder_id"\s*:\s*"[^"]*"',
                          '"_log_finder_id":"%s"' % my_fid, body, count=1)
        headers = {k: v for k, v in (tpl.get("headers") or {}).items()
                   if k.lower() not in ("cookie", "content-length", "host", "connection")}
        headers["Cookie"] = jar

        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body.encode("utf-8"),
                                         headers=headers, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as r:
                txt = r.read().decode("utf-8", "replace")
            cost = (time.time() - t0) * 1000
            d = json.loads(txt)
            data = d.get("data") or {}
            print("%-14s %6.0fms errCode=%-4s nick=%-22s status=%s"
                  % (aid, cost, d.get("errCode"), (data.get("wxNickname") or "-")[:22],
                     data.get("status")))
            print("               期望昵称: %s" % (acc.get("_wx_name") or acc.get("name") or "-"))
        except Exception as e:
            print("%-14s 失败: %r" % (aid, e))


if __name__ == "__main__":
    main()
