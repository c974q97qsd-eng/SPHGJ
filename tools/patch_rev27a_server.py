# -*- coding: utf-8 -*-
"""rev27a: 服务端接入远程同步浏览(WS 串流 + 输入回传)。"""
import io
import os

LOCAL = r"D:\SPHGJ"
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# 1) 导入 + 实例化
add(r"backend\server.py",
    "from .publish_queue import PublishQueue\nfrom . import restart as restart_mod",
    "from .publish_queue import PublishQueue\nfrom .remote_view import RemoteRegistry\nfrom . import restart as restart_mod",
    "import RemoteRegistry")

add(r"backend\server.py",
    """publish_queue = PublishQueue(manager, storage, config, emit=hub.emit, save_config=save_config)""",
    """publish_queue = PublishQueue(manager, storage, config, emit=hub.emit, save_config=save_config)
# rev27: 远程同步浏览(CDP 串流),「打开」按钮走这里,5 分钟无操作自动断开
remote_registry = RemoteRegistry(manager, config, storage)""",
    "实例化 registry")

# 2) WS 端点 + 辅助接口(插在系统重启接口之前)
add(r"backend\server.py",
    '@app.post("/api/system/restart")\nasync def system_restart(delay: float = Query(3.0, ge=1, le=60)):',
    '''@app.websocket("/api/remote/ws")
async def remote_ws(ws: WebSocket, account: str = Query(""), url: str = Query("")):
    """远程同步浏览通道(rev27)。

    文本帧 = JSON 指令/元信息;二进制帧 = JPEG 画面。
    客户端 -> 服务端:{t:"mouse"|"text"|"key"|"nav"|"ping"|"close"}
    服务端 -> 客户端:{t:"opening"|"hello"|"url"|"closed"|"error"} + 二进制 JPEG

    会话期间该账号 worker 会让出 profile 锁(数据抓取暂停),断开/空闲超时后自动恢复。
    """
    await ws.accept()
    if not account:
        try:
            await ws.send_json({"t": "error", "message": "缺少 account 参数"})
            await ws.close()
        except Exception:
            pass
        return
    sess = None
    try:
        await ws.send_json({"t": "opening", "account_id": account})
        sess, reused = await remote_registry.open(account, url or None)
        await ws.send_json(sess.meta())
    except Exception as e:
        logger.warning(f"[remote] 建立会话失败 {account}: {e}")
        try:
            await ws.send_json({"t": "error",
                                "message": f"{type(e).__name__}: {str(e)[:200]}"})
            await ws.close()
        except Exception:
            pass
        return

    async def reader():
        while True:
            msg = await ws.receive_json()
            t = msg.get("t")
            if t == "mouse":
                await sess.mouse(msg.get("kind") or "move", msg.get("x") or 0,
                                msg.get("y") or 0, msg.get("button") or "left",
                                dy=msg.get("dy") or 0, dx=msg.get("dx") or 0)
            elif t == "text":
                await sess.text(msg.get("text") or "")
            elif t == "key":
                await sess.key(msg.get("name") or "")
            elif t == "nav":
                await sess.nav(msg.get("action") or "", msg.get("url") or "")
            elif t == "close":
                return
            elif t == "ping":
                pass          # 只保活,不刷新「用户动作」时间(否则空闲超时永不触发)

    async def pusher():
        while not sess.closed:
            jpeg = await sess.next_frame(timeout=0.8)
            if jpeg:
                await ws.send_bytes(jpeg)
            if sess.closed:
                break

    async def urlwatch():
        last = ""
        while not sess.closed:
            await asyncio.sleep(1.2)
            cur = await sess.refresh_url()
            if cur and cur != last:
                last = cur
                await ws.send_json({"t": "url", "url": cur})

    tasks = [asyncio.create_task(reader()), asyncio.create_task(pusher()),
             asyncio.create_task(urlwatch())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
    except Exception as e:
        logger.debug(f"[remote] WS 循环异常: {e}")
    finally:
        reason = sess.close_reason or ("idle" if sess.closed else "client")
        try:
            await remote_registry.close(account, reason)
        except Exception:
            pass
        try:
            await ws.send_json({"t": "closed", "reason": reason})
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/remote/sessions")
async def remote_sessions():
    """当前串流会话(用于界面显示「谁在占用」)。"""
    return {"items": remote_registry.snapshot()}


@app.post("/api/remote/close")
async def remote_close(account: str = Query("")):
    """手动断开某账号的串流并恢复抓取。"""
    if not account:
        raise HTTPException(400, "缺少 account 参数")
    await remote_registry.close(account, "manual")
    return {"ok": True}


@app.post("/api/system/restart")
async def system_restart(delay: float = Query(3.0, ge=1, le=60)):''',
    "WS 端点与远程会话接口")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")

    print("\n== 自校验 ==")
    import ast
    src = io.open(os.path.join(LOCAL, "backend", "server.py"), encoding="utf-8").read()
    ast.parse(src)
    print("  OK  AST server.py")
    for needle, tag in (("RemoteRegistry", "registry"),
                        ('@app.websocket("/api/remote/ws")', "WS 路由"),
                        ("/api/remote/sessions", "会话列表"),
                        ("/api/remote/close", "关闭接口")):
        print(f"  {'OK ' if needle in src else 'BAD'} {tag}")
    print("\n[DONE]")


if __name__ == "__main__":
    apply()
