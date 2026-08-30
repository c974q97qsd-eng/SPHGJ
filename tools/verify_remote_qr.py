"""端到端验证:网页二维码模式(headless)能否推送二维码到 WS。

用法: python tools/verify_remote_qr.py
"""
import sys
import os
import json
import base64
import asyncio
import urllib.request

import websockets

BASE = "http://127.0.0.1:8712"
WS = "ws://127.0.0.1:8712/ws"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def http_post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def main():
    async with websockets.connect(WS) as ws:
        login = await asyncio.to_thread(http_post, "/api/accounts/login/start?headed=false")
        print(f"[1] login_start -> sid={login.get('sid')} status={login.get('status')} headed={login.get('headed')}")
        sid = login["sid"]
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 75
        qr = None
        last_status = None
        while loop.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            data = json.loads(msg)
            ev, payload = data.get("event"), data.get("payload", {})
            if payload.get("sid") != sid:
                continue
            if ev == "login_status":
                st = payload.get("status")
                if st != last_status:
                    print(f"    login_status: {st}")
                    last_status = st
                if st in ("failed", "captured", "cancelled"):
                    break
            elif ev == "qr_update":
                qr = payload.get("image")
                break
        if qr:
            raw = base64.b64decode(qr.split(",", 1)[1])
            p = os.path.join(ROOT, ".workbuddy", "verify_qr.png")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(raw)
            print(f"[2] 收到二维码! dataURL={len(qr)}字符 解码={len(raw)}字节 -> {p}")
        else:
            print("[2] 未收到二维码(qr_update)")
        await asyncio.to_thread(http_post, f"/api/accounts/login/{sid}/cancel")
        print("[3] 已 cancel 清理会话")


if __name__ == "__main__":
    asyncio.run(main())
