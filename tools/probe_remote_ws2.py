# -*- coding: utf-8 -*-
"""实测远程串流 v2:等页面渲染完 -> 存最新帧 -> 发点击/输入 -> 看帧率与画面变化。"""
import asyncio
import json
import os
import sys
import time

import websockets

API = "192.168.1.27:8712"
ACC = sys.argv[1] if len(sys.argv) > 1 else "uspoloassn4"
OUT = os.path.join(os.environ.get("TEMP", "."), "_rv_shot2.jpg")
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 40


async def main():
    uri = f"ws://{API}/api/remote/ws?account={ACC}"
    print("连接", uri)
    t0 = time.time()
    async with websockets.connect(uri, max_size=8 * 1024 * 1024) as ws:
        frames = 0
        last = None
        total_bytes = 0
        meta = None
        clicked = False
        while time.time() - t0 < WAIT:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                msg = None
            if isinstance(msg, (bytes, bytearray)):
                frames += 1
                total_bytes += len(msg)
                last = msg
                continue
            if msg:
                d = json.loads(msg)
                t = d.get("t")
                if t == "opening":
                    print(f"  opening (t={time.time()-t0:.1f}s)")
                elif t == "hello":
                    meta = d
                    print(f"  hello {d.get('account_name')} {d.get('w')}x{d.get('h')} "
                          f"url={str(d.get('url'))[:60]} idle={d.get('idle_timeout')}s "
                          f"(t={time.time()-t0:.1f}s)")
                elif t == "url":
                    print("  url ->", str(d.get("url"))[:70])
                elif t == "error":
                    print("  ERROR:", d.get("message"))
                    return
                elif t == "closed":
                    print("  closed:", d.get("reason"))
                    break
            # 页面渲染需要时间:8s 后再点,点完看有没有新帧
            if meta and not clicked and time.time() - t0 > 8:
                clicked = True
                f0 = frames
                await ws.send(json.dumps({"t": "mouse", "kind": "down", "x": 300, "y": 200}))
                await ws.send(json.dumps({"t": "mouse", "kind": "up", "x": 300, "y": 200}))
                await asyncio.sleep(0.5)
                await ws.send(json.dumps({"t": "text", "text": "hello"}))
                print(f"  已发送点击+文本(此前 {frames} 帧)")
                await asyncio.sleep(3)
                print(f"  输入后新增 {frames - f0} 帧")
                if last:
                    open(OUT, "wb").write(last)
                    print(f"  当前帧已存 {len(last)}B -> {OUT}")
        if last:
            open(OUT, "wb").write(last)
        dur = time.time() - t0
        print(f"总计 {frames} 帧 / {dur:.0f}s = {frames/dur:.1f}fps, "
              f"流量 {total_bytes/1024/1024:.1f}MB ({total_bytes/1024/dur:.0f}KB/s)")
        print("末帧 ->", OUT, os.path.getsize(OUT) if os.path.exists(OUT) else 0, "B")


asyncio.run(main())
