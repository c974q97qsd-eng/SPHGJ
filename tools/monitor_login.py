"""监听扫码登录全过程事件(WS),写文件供排查。

用法: python tools/monitor_login.py [seconds]
输出: .workbuddy/login_monitor.log  + stdout
"""
import asyncio
import json
import os
import sys
import time

import websockets

PORT = int(os.environ.get("SPHGJ_PORT", "8712"))
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".workbuddy", "login_monitor.log")
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 900.0

INTERESTING = {"login_status", "qr_update", "account_select"}


def short(v, n=400):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + f"...(+{len(s)-n})"


async def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    f = open(OUT, "a", encoding="utf-8")
    url = f"ws://127.0.0.1:{PORT}/ws"
    t0 = time.time()

    def log(line):
        stamp = time.strftime("%H:%M:%S")
        out = f"[{stamp} +{time.time()-t0:6.1f}s] {line}"
        print(out, flush=True)
        f.write(out + "\n")
        f.flush()

    log(f"=== 监听启动 {url} (最长 {DURATION:.0f}s) ===")
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            log("WS 已连接")
            while time.time() - t0 < DURATION:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    log(f"RAW {short(raw, 200)}")
                    continue
                if isinstance(msg, list):  # 连接时的批量推送
                    for m in msg:
                        if isinstance(m, dict) and m.get("event") in INTERESTING:
                            log(f"{m['event']}: {short(m.get('payload'))}")
                    continue
                ev = msg.get("event")
                if ev in INTERESTING:
                    log(f"{ev}: {short(msg.get('payload'))}")
                elif ev:
                    log(f"  (skip {ev})")
    except Exception as e:
        log(f"WS 错误: {type(e).__name__}: {e}")
    finally:
        log("=== 监听结束 ===")
        f.close()


asyncio.run(main())
