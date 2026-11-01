# -*- coding: utf-8 -*-
"""rev61 真机验证：对一条真实待回复客户评论发一次 reply，看 exportId 是否解决问题。"""
import io
import json
import urllib.request

FID = "v2_060000231003b20faec8c4e78e1dc0d4ce0ce536b07781a00f0fc46bd35abf75f03bb308f4b0@finder"
CID = "15045101693521758355"
ACC = "uspoloassn8"
TEXT = "鞋底细节我私信发您实拍图，点我头像进直播间，客服可以给您讲清楚"

body = json.dumps({"finder_id": FID, "account_id": ACC, "content": TEXT},
                  ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(
    "http://192.168.1.27:8712/api/comments/%s/reply" % CID,
    data=body, method="POST",
    headers={"Content-Type": "application/json; charset=utf-8"})
print("POST /api/comments/%s/reply" % CID)
print("content:", TEXT)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        print("HTTP", r.status)
        print("resp:", io.open(0).read() if False else r.read().decode("utf-8", "replace"))
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print("resp:", e.read().decode("utf-8", "replace"))
except Exception as e:
    print("EXC", repr(e))
