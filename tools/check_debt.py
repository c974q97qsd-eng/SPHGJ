# -*- coding: utf-8 -*-
"""查 9 条欠账评论的归属账号 + 各账号在线状态。"""
import json
import sqlite3
import datetime
import urllib.request

DB = r"\\Sphgj\sphgj\data\sphgj.db"
IDS = ("15031461443450048240", "15034213234021174745", "15035562706612455662",
       "15035622547314772197", "15037042521035902068", "15039901451940858780",
       "15041648371875847664", "15043180942665780780", "15043189290312338973")

c = sqlite3.connect("file://" + DB.replace("\\", "/") + "?mode=ro&immutable=1", uri=True)
d = json.load(urllib.request.urlopen("http://192.168.1.27:8712/api/accounts", timeout=10))
accs = d if isinstance(d, list) else (d.get("accounts") or [])
state = {a["id"]: (a.get("logged_in"), a.get("auth_state")) for a in accs}

for cid in IDS:
    r = c.execute("SELECT comment_id,account_id,finder_id,create_time,nickname,content "
                  "FROM comments WHERE comment_id=?", (cid,)).fetchone()
    t = datetime.datetime.fromtimestamp(r[3]).strftime("%m-%d %H:%M")
    print(" %s | %s | 账号=%-12s 在线=%s | %s | %s" %
          (t, cid, r[1], state.get(r[1]), r[4], (r[5] or "")[:26]))
