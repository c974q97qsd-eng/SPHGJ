# -*- coding: utf-8 -*-
"""实测远程串流:连 WS -> 拿帧存图 -> 发点击/输入 -> 看页面变化 -> 关闭。

用 uspoloassn4(优选仓,当前未运行):会话结束后不会去恢复 worker,无副作用。
"""
import asyncio
import json
import os
import sys
import time

import websockets

API = "192.168.1.27:8712"
ACC = sys.argv[1] if len(sys.argv) > 1 else "uspoloassn4"
OUT = os.path.join(os.environ.get("TEMP", "."), "_rv_shot.jpg")


async def main():
    uri = f"ws://{API}/api/remote/ws?account={ACC}"
    print("连接", uri)
    t0 = time.time()
    async with websockets.connect(uri, max_size=8 * 1024 * 1024) as ws:
        frames = 0
        saved = 0
        meta = None
        urls = []
        deadline = time.time() + 90
        clicked = False
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                print("  (5s 无消息)")
                continue
            if isinstance(msg, (bytes, bytearray)):
                frames += 1
                if not saved:
                    open(OUT, "wb").write(msg)
                    saved = 1
                    print(f"  首帧 {len(msg)}B -> {OUT} (t={time.time()-t0:.1f}s)")
                if frames % 20 == 0:
                    print(f"  已收 {frames} 帧 (t={time.time()-t0:.1f}s)")
                continue
            d = json.loads(msg)
            t = d.get("t")
            if t == "opening":
                print("  opening…")
            elif t == "hello":
                meta = d
                print(f"  hello: {d.get('account_name')} {d.get('w')}x{d.get('h')} "
                      f"url={str(d.get('url'))[:70]} idle={d.get('idle_timeout')}s "
                      f"(t={time.time()-t0:.1f}s)")
            elif t == "url":
                urls.append(d.get("url"))
                print("  url ->", str(d.get("url"))[:80])
            elif t == "error":
                print("  ERROR:", d.get("message"))
                return
            elif t == "closed":
                print("  closed:", d.get("reason"))
                break
            if meta and not clicked and frames >= 3:
                clicked = True
                # 点一下页面中间,再输入一段文字,确认输入通道可用
                await ws.send(json.dumps({"t": "mouse", "kind": "down", "x": 640, "y": 400}))
                await ws.send(json.dumps({"t": "mouse", "kind": "up", "x": 640, "y": 400}))
                await asyncio.sleep(0.4)
                await ws.send(json.dumps({"t": "text", "text": "hello"}))
                print("  已发送点击 + 文本输入")
                f0 = frames
                await asyncio.sleep(2.5)
                print(f"  输入后新帧 {frames - f0} 帧(>0 说明画面有响应)")
            if clicked and time.time() - t0 > 40:
                await ws.send(json.dumps({"t": "close"}))
                print("  主动关闭")
                break
        print(f"总计 {frames} 帧,{len(urls)} 次 url 通知")


asyncio.run(main())
