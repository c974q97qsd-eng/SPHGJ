# -*- coding: utf-8 -*-
"""补发 10 月 300800 故障期间漏回的 9 条客户评论。

文案按场景分三类，不编价格、不承诺具体售后政策，统一引导进直播间。
逐条 POST /api/comments/{id}/reply，失败保留原状（新代码不会误标 replied）。
"""
import io
import json
import time
import sqlite3
import datetime
import urllib.request
import urllib.error

DB = r"\\Sphgj\sphgj\data\sphgj.db"
API = "http://192.168.1.27:8712/api/comments/%s/reply"

# 场景 A 问价 / B 实物一致 / C 季节款 / AB 价格+实物
PLAN = [
    ("15031461443450048240", "B", "视频都是实拍的，收到货一样，点我头像进直播间能看细节"),
    ("15034213234021174745", "B", "视频都是实拍的，收到货一样，点我头像进直播间能看细节"),
    ("15035562706612455662", "C", "有冬天的加绒款，点我头像进直播间看具体款式"),
    ("15035622547314772197", "C", "有冬天的加绒款，点我头像进直播间看具体款式"),
    ("15037042521035902068", "B", "视频都是实拍的，收到货一样，点我头像进直播间能看细节"),
    ("15039901451940858780", "A", "价格点我头像进直播间，能领优惠券更划算"),
    ("15041648371875847664", "AB", "价格点我头像进直播间看还能领券，视频实拍，收到一样"),
    ("15043180942665780780", "A", "价格点我头像进直播间，能领优惠券更划算"),
    ("15043189290312338973", "A", "价格点我头像进直播间，能领优惠券更划算"),
]

c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
log = []
ok = fail = 0

for cid, kind, text in PLAN:
    r = c.execute("SELECT finder_id,account_id,nickname,content,create_time "
                  "FROM comments WHERE comment_id=?", (cid,)).fetchone()
    fid, acc, nick, content, ts = r
    t = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"

    body = json.dumps({"finder_id": fid, "account_id": acc, "content": text},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API % cid, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        result = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        result = repr(e)
        code = -1

    good = (code == 200 and '"ok"' in result)
    ok += 1 if good else 0
    fail += 0 if good else 1
    line = "%s %s | %s | %s | %s -> %s" % (
        "OK  " if good else "FAIL", t, cid, nick, (content or "")[:20],
        result[:120] if not good else text[:24])
    print(line, flush=True)
    log.append(line)
    time.sleep(4)

print("\n补发完成: 成功 %d / 失败 %d" % (ok, fail))
io.open(r"D:\SPHGJ\tools\_debt_result.txt", "w", encoding="utf-8").write("\n".join(log))
