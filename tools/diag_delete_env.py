# -*- coding: utf-8 -*-
"""只读核查：母本代码版本 / risk_control 配置 / 今日其它写操作来源。"""
import hashlib
import io
import json
import os
import sqlite3

LOCAL = r"D:\SPHGJ"
MASTER = r"\\Sphgj\sphgj"
OUT = r"D:\SPHGJ\_diag_del_env.txt"
FILES = (
    r"backend\api_client.py",
    r"backend\account_manager.py",
    r"backend\server.py",
)
SNAP = r"D:\SPHGJ\_db_snap\sphgj.db"


def norm_hash(path):
    raw = open(path, "rb").read().replace(b"\r\n", b"\n")
    return hashlib.md5(raw).hexdigest()


def main():
    out = []
    out.append("=== 本地 vs 母本 代码一致性（CRLF 归一化后 md5）===")
    for rel in FILES:
        a, b = os.path.join(LOCAL, rel), os.path.join(MASTER, rel)
        try:
            ha, hb = norm_hash(a), norm_hash(b)
            out.append("%-32s %s  local=%s master=%s" %
                       (rel, "SAME" if ha == hb else "DIFF", ha[:10], hb[:10]))
            if ha != hb:
                out.append("   local_size=%d master_size=%d" %
                           (os.path.getsize(a), os.path.getsize(b)))
        except Exception as e:
            out.append("%-32s ERR %r" % (rel, e))

    out.append("")
    out.append("=== 母本 config.json: risk_control ===")
    try:
        cfg = json.load(io.open(os.path.join(MASTER, "config.json"), encoding="utf-8"))
        rc = cfg.get("risk_control") or {}
        for k in ("api_read_via", "api_write_via", "fetch_parallel",
                  "write_lock_scope", "daily_write_limit", "read_interval",
                  "write_interval", "write_http_accounts"):
            out.append("  %s = %r" % (k, rc.get(k, "(未设置,走代码默认)")))
    except Exception as e:
        out.append("  ERR " + repr(e))

    out.append("")
    out.append("=== 今日其它写操作线索 ===")
    try:
        c = sqlite3.connect(SNAP, timeout=10)
        for t in ("own_comments", "auto_commented", "own_comment_pending"):
            cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t).fetchall()]
            out.append("  %s 列: %s" % (t, ", ".join(cols)))
            tcol = next((x for x in ("created_at", "ts", "replied_at", "time") if x in cols), None)
            if tcol:
                rows = c.execute(
                    "SELECT %s FROM %s WHERE %s >= '2026-11-16' ORDER BY %s DESC LIMIT 6"
                    % (tcol, t, tcol, tcol)).fetchall()
                out.append("    今日最近: %s" % repr(rows))
        c.close()
    except Exception as e:
        out.append("  ERR " + repr(e))

    io.open(OUT, "w", encoding="utf-8").write("\n".join(out))
    print("wrote " + OUT)


if __name__ == "__main__":
    main()
