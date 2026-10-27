# -*- coding: utf-8 -*-
"""rev48 单测: 纯 HTTP 探测的纯函数部分(cookie 过滤 / 规格借用 / timestamp 刷新)。

不需要网络,不启浏览器。跑法:
    python tools/test_rev48_unit.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import live_probe_http as lph

FAIL = []


def chk(cond, label, detail=""):
    print(("  PASS " if cond else "  FAIL ") + label + (("  <- " + str(detail)) if not cond else ""))
    if not cond:
        FAIL.append(label)


print("== 1. cookie 域过滤 ==")
state = {"cookies": [
    {"name": "sessionid", "value": "A", "domain": "channels.weixin.qq.com"},
    {"name": "wxuin", "value": "B", "domain": "channels.weixin.qq.com"},
    {"name": "wxuin", "value": "WRONG", "domain": "developers.weixin.qq.com"},
    {"name": "bid", "value": "C", "domain": "developers.weixin.qq.com"},
    {"name": "rootcookie", "value": "D", "domain": ".weixin.qq.com"},
    {"name": "evil", "value": "E", "domain": "evil-weixin.qq.com"},
]}
jar = lph.build_cookie_header(state)
names = [p.split("=", 1)[0] for p in jar.split("; ") if p]
chk("sessionid=A" in jar, "channels 域的 sessionid 保留")
chk("wxuin=B" in jar, "channels 域的 wxuin 保留")
chk("WRONG" not in jar, "developers 域的同名 wxuin 被丢弃(避免重复/串号)", jar)
chk("bid=" not in jar, "developers 域的 bid 被丢弃")
chk("rootcookie=D" in jar, "父域 .weixin.qq.com 的 cookie 保留(浏览器也会发)")
chk("evil" not in jar, "evil-weixin.qq.com 这种伪后缀被丢弃")
chk(names.count("wxuin") == 1, "wxuin 不重复", names)
chk(lph.build_cookie_header({}) == "", "空 storage_state -> 空 cookie 头")
chk(lph.build_cookie_header(None) == "", "None -> 空 cookie 头")

print()
print("== 2. prepare_request: timestamp 刷新 + 借用时换账号标识 ==")
tpl = {
    "url": "https://channels.weixin.qq.com/x/check_live_status?_aid=OLD-AID&_rid=abc",
    "method": "POST",
    "headers": {"Content-Type": "application/json", "Cookie": "should-be-dropped",
                "Content-Length": "999", "Host": "wrong"},
    "postData": '{"a":1,"timestamp":"1111111111111","_log_finder_id":"OLD-FID","b":2}',
}
acc = {"id": "accX", "_aid": "NEW-AID", "_log_finder_id": "NEW-FID"}
url, method, headers, body = lph.prepare_request(tpl, acc, borrowed=True)
chk("_aid=NEW-AID" in url and "OLD-AID" not in url, "借用时 URL 的 _aid 换成自己的", url)
chk("_log_finder_id\":\"NEW-FID" in body.replace(" ", ""), "借用时 body 的 _log_finder_id 换成自己的", body)
chk('"1111111111111"' not in body, "timestamp 已被刷新", body)
chk(abs(int(body.split('"timestamp":"')[1].split('"')[0]) - time.time() * 1000) < 60000,
    "新 timestamp 是当前毫秒")
chk("Cookie" not in headers, "规格里的 Cookie 被剔除(由 storage_state 单独拼)")
chk("Content-Length" not in headers, "Content-Length 交给 HTTP 客户端")
chk("Host" not in headers, "Host 交给 HTTP 客户端")
chk(method == "POST", "method 保持 POST")

# 非借用:原样保留本账号规格里的标识(已实测可用,不做替换以免引入新变量)
acc2 = {"id": "accY", "_aid": "SOME-OTHER", "_log_finder_id": "SOME-OTHER-FID"}
url2, _, _, body2 = lph.prepare_request(tpl, acc2, borrowed=False)
chk("_aid=OLD-AID" in url2, "非借用时 URL 的 _aid 原样不动", url2)
chk("OLD-FID" in body2, "非借用时 _log_finder_id 原样不动", body2)

print()
print("== 3. 规格借用优先级 ==")
lph._borrow_spec = lambda aid: {"url": "u", "method": "POST"}
spec, borrowed = lph.load_live_status_spec("__no_such_account__")
chk(borrowed is True and spec and spec.get("url") == "u", "本账号无规格 -> 借模板(borrowed=True)")

print()
print("== 4. judge_live 兼容(status=0 判未播) ==")
from backend.live_fetcher import judge_live
live, why = judge_live(status=0)
chk(live is False and why == "status=0", "status=0 -> 未播", (live, why))
live2, why2 = judge_live(stream_url="http://x/a.flv", status=0)
chk(live2 is True and why2 == "stream_url", "有流地址 -> 在播(优先于 status)", (live2, why2))
live3, why3 = judge_live(status=1)
chk(live3 is False and why3 == "no_signal", "status=1 目前不认定为在播(未验证过的取值,不猜)", (live3, why3))

print()
if FAIL:
    print("FAILED %d 项: %s" % (len(FAIL), FAIL))
    sys.exit(1)
print("ALL PASS")
