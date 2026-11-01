# -*- coding: utf-8 -*-
"""只读:查母本账号的写通道配置 / aid / cookie / 自动评论开关。"""
import io
import json

P = "\\\\Sphgj\\sphgj\\config.json"
d = json.load(io.open(P, encoding="utf-8"))
rc = d.get("risk_control") or {}
print("api_read_via =", rc.get("api_read_via"),
      "| api_write_via =", rc.get("api_write_via"),
      "| write_http_accounts =", rc.get("api_write_http_accounts"))
print("daily_write_limit =", rc.get("daily_write_limit"))
print()
for a in (d.get("accounts") or []):
    ss = a.get("storage_state") or {}
    nck = len(ss.get("cookies") or []) if isinstance(ss, dict) else 0
    has = lambda k: "Y" if (ss.get("origins") if k == "ls" else nck) else "N"
    print("%-14s aid=%s cookies=%s ls=%s auto_comment=%s content=%r logged_in=%s" % (
        a.get("id"), (a.get("_aid") or "-")[:12], nck, has("ls"),
        bool(a.get("auto_comment_enabled")),
        (a.get("auto_comment_content") or "")[:18], a.get("logged_in")))
