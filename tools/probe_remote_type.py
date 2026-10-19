# -*- coding: utf-8 -*-
"""实测交互:点「视频描述」输入框 -> 输入文字 -> 回读画面确认文字真的进去了。"""
import asyncio
import json
import os
import sys
import time

import websockets

API = "192.168.1.27:8712"
ACC = sys.argv[1] if len(sys.argv) > 1 else "uspoloassn4"
# 描述框在图上的位置(1280x800 viewport):x≈870 y≈245
TX, TY = 870, 245
OUT = os.path.join(os.environ.get("TEMP", "."), "_rv_type.jpg")


async def main():
    uri = f"ws://{API}/api/remote/ws?account={ACC}"
    frames = 0
    last = None
    t0 = time.time()
    async with websockets.connect(uri, max_size=8 * 1024 * 1024) as ws:
        async def drain(sec):
            nonlocal frames, last
            end = time.time() + sec
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
                except asyncio.TimeoutError:
                    break
                if isinstance(m, (bytes, bytearray)):
                    frames += 1
                    last = m
                else:
                    d = json.loads(m)
                    if d.get("t") == "error":
                        print("  ERROR:", d.get("message"))
                    elif d.get("t") == "closed":
                        print("  closed:", d.get("reason"))

        await drain(10)                      # 等页面渲染
        print(f"  渲染后 {frames} 帧")
        if last:
            open(os.path.join(os.environ.get("TEMP", "."), "_rv_before.jpg"), "wb").write(last)

        # 点描述框
        await ws.send(json.dumps({"t": "mouse", "kind": "down", "x": TX, "y": TY}))
        await ws.send(json.dumps({"t": "mouse", "kind": "up", "x": TX, "y": TY}))
        await drain(2)
        f1 = frames
        print(f"  点击后 {frames} 帧(+{frames-f1 if False else frames-f1})")

        # 输入文字
        await ws.send(json.dumps({"t": "text", "text": "远程输入测试 hello 123"}))
        await drain(4)
        print(f"  输入后总帧 {frames}")
        if last:
            open(OUT, "wb").write(last)
            print(f"  末帧已存 {len(last)}B -> {OUT}")

        await ws.send(json.dumps({"t": "close"}))
    print(f"耗时 {time.time()-t0:.0f}s / {frames} 帧")


asyncio.run(main())
