# -*- coding: utf-8 -*-
"""rev56c 补丁:server 加抖音（platform=dy）路由。

  GET  /api/dy/status                     抖音侧状态
  POST /api/dy/attach    {port:9222}      把已登录的 Chromium 挂上来（需 dy_dev_attach=true）
  POST /api/dy/detach                     断开挂载
  POST /api/dy/fetch     {max_works?}     立刻抓一轮评论
  POST /api/dy/comments/{cid}/reply   {aweme_id, text}
  POST /api/dy/comments/{cid}/delete  {aweme_id}

用法: python tools/patch_rev56c.py
"""
import io
import sys

P = r"D:\SPHGJ\backend\server.py"

IMPORT_ANCHOR = "from .browser import reap_profile_orphans\n"
IMPORT_NEW = ("from .browser import reap_profile_orphans\n"
              "from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）\n")

CREAT_ANCHOR = "manager = AccountManager(config, storage, emit=hub.emit)\n"
CREAT_NEW = ("manager = AccountManager(config, storage, emit=hub.emit)\n"
             "# rev56: 抖音侧运行容器。它**不接管登录/浏览器启停** —— 抖音的扫码流程和\n"
             "# 视频号完全不同，塞进 AccountWorker 会把已经稳定的在网状态机搞乱。\n"
             "# 页面通过 set_page 注入（见 /api/dy/attach），没注入时所有网络方法都返回未就绪。\n"
             "dy_manager = DouyinManager(config, storage, emit=hub.emit, save_config=save_config)\n"
             "_dy_cdp = {}   # {pw, browser} —— 挂载的外部浏览器句柄，别让 GC 收掉\n")

ROUTE_ANCHOR = '@app.post("/api/comments/batch-delete")\n'

ROUTES = '''# ==================== rev56: 抖音（platform=dy）====================


@app.get("/api/dy/status")
async def dy_status():
    """抖音侧状态：有没有配账号 / 页面是否就绪 / 已抓评论数 / 上次抓取摘要。

    页面未就绪时 comments_total 等仍会返回 —— 数据是库里已有的，和浏览器无关。
    """
    return dy_manager.status()


@app.post("/api/dy/attach")
async def dy_attach(request: Request):
    """把一个**已登录** creator.douyin.com 的 Chromium 挂上来（CDP 调试端口）。

    为什么需要这个入口：抖音的扫码登录流程还没做（下一步的事），但抓取/回复的
    能力已经可用。先用「你自己开着调试端口登录好，再挂上来」的方式跑通全链路，
    比自己从零做一遍抖音扫码要快，也不会为了赶工把登录逻辑写歪。

    安全：必须 config.json 里 `"dy_dev_attach": true` 才允许，默认关。
    请求体：{"port": 9222}
    """
    if config.get("dy_dev_attach") is not True:
        raise HTTPException(403, "未开启 dy_dev_attach（在 config.json 里设 true 才允许挂载外部浏览器）")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        port = int((body or {}).get("port") or 9222)
    except (TypeError, ValueError):
        raise HTTPException(400, "port 必须是数字")
    if _dy_cdp.get("browser") is not None:
        raise HTTPException(409, "已经挂着一个浏览器了，先 /api/dy/detach")
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        raise HTTPException(500, f"当前环境没有 playwright，无法挂载: {e!r}")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception as e:
        await pw.stop()
        raise HTTPException(502, f"连不上 127.0.0.1:{port}（需用 --remote-debugging-port={port} 启动）: {e!r}")
    page = None
    for c in browser.contexts:
        for p in c.pages:
            if "creator.douyin.com" in (p.url or ""):
                page = p
                break
        if page is not None:
            break
    if page is None:
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://creator.douyin.com/creator-micro/content/manage")
        await page.wait_for_timeout(6000)
    _dy_cdp["pw"], _dy_cdp["browser"] = pw, browser
    dy_manager.set_page(page)
    ident = await dy_manager.refresh_identity()
    return {"ok": True, "url": page.url,
            "uid": ident.get("uid"), "sec_uid": ident.get("sec_uid"),
            "nickname": ident.get("nickname"),
            "status": dy_manager.status()}


@app.post("/api/dy/detach")
async def dy_detach():
    """断开挂载。只关我们这边的连接，**不关你的浏览器**（里面可能还开着别的业务）。"""
    dy_manager.set_page(None)
    br, pw = _dy_cdp.get("browser"), _dy_cdp.get("pw")
    _dy_cdp.clear()
    for obj in (br, pw):
        if obj is None:
            continue
        try:
            await obj.close() if obj is br else await obj.stop()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/dy/fetch")
async def dy_fetch(request: Request):
    """立刻抓一轮抖音评论。请求体可带 {"max_works": N} 限制本轮作品数（调试用）。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    mw = (body or {}).get("max_works")
    try:
        mw = int(mw) if mw else None
    except (TypeError, ValueError):
        mw = None
    rep = await dy_manager.fetch_now(max_works=mw)
    if not rep.get("__ok"):
        code = 503 if rep.get("__err") == "not_ready" else 502
        raise HTTPException(code, rep.get("__msg") or rep.get("__err"))
    # new_comments 可能上千条，接口只回摘要，明细走 /api/comments?finder_id=
    out = {k: v for k, v in rep.items() if k != "new_comments"}
    out["new_samples"] = (rep.get("new_comments") or [])[:20]
    return out


@app.post("/api/dy/comments/{comment_id}/reply")
async def dy_reply(comment_id: str, request: Request):
    """回复抖音评论。请求体 {"aweme_id": "...", "text": "..."}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    text = str((body or {}).get("text") or "").strip()
    aweme_id = str((body or {}).get("aweme_id") or "").strip()
    if not text:
        raise HTTPException(400, "回复内容不能为空")
    if not aweme_id:
        raise HTTPException(400, "缺少 aweme_id（评论列表里每条都带 export_id）")
    env = await dy_manager.reply(comment_id, aweme_id, text)
    if not env.get("__ok"):
        raise HTTPException(502, env.get("__msg") or env.get("__err") or "回复失败")
    return {"ok": True, "comment_id": comment_id}


@app.post("/api/dy/comments/{comment_id}/delete")
async def dy_delete_comment(comment_id: str, request: Request):
    """删除抖音评论。请求体 {"aweme_id": "..."}"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    aweme_id = str((body or {}).get("aweme_id") or "").strip()
    if not aweme_id:
        raise HTTPException(400, "缺少 aweme_id")
    env = await dy_manager.delete_comment(comment_id, aweme_id)
    if not env.get("__ok"):
        raise HTTPException(502, env.get("__msg") or env.get("__err") or "删除失败")
    return {"ok": True, "comment_id": comment_id}


'''


def main():
    s = io.open(P, encoding="utf-8").read()
    ok = True

    if "from .douyin.manager import DouyinManager" in s:
        print("  [跳过] import 已存在")
    else:
        assert s.count(IMPORT_ANCHOR) == 1, s.count(IMPORT_ANCHOR)
        s = s.replace(IMPORT_ANCHOR, IMPORT_NEW, 1)
        print("  [ok] import")

    if "dy_manager = DouyinManager(" in s:
        print("  [跳过] dy_manager 已存在")
    else:
        assert s.count(CREAT_ANCHOR) == 1, s.count(CREAT_ANCHOR)
        s = s.replace(CREAT_ANCHOR, CREAT_NEW, 1)
        print("  [ok] dy_manager 创建")

    if '@app.get("/api/dy/status")' in s:
        print("  [跳过] 路由已存在")
    else:
        assert s.count(ROUTE_ANCHOR) == 1, s.count(ROUTE_ANCHOR)
        s = s.replace(ROUTE_ANCHOR, ROUTES + ROUTE_ANCHOR, 1)
        print("  [ok] 6 条路由")

    io.open(P, "w", encoding="utf-8", newline="").write(s)

    checks = [
        ("from .douyin.manager import DouyinManager" in s, "import"),
        ("dy_manager = DouyinManager(" in s, "dy_manager"),
        ('@app.get("/api/dy/status")' in s, "status 路由"),
        ('@app.post("/api/dy/attach")' in s, "attach 路由"),
        ('@app.post("/api/dy/detach")' in s, "detach 路由"),
        ('@app.post("/api/dy/fetch")' in s, "fetch 路由"),
        ("/api/dy/comments/{comment_id}/reply" in s, "reply 路由"),
        ("/api/dy/comments/{comment_id}/delete" in s, "delete 路由"),
        ("dy_dev_attach" in s, "attach 有开关保护"),
    ]
    bad = [k for okk, k in checks if not okk]
    print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
    try:
        compile(s, P, "exec")
        print("语法 OK")
    except SyntaxError as e:
        print("语法错误:", e)
        return 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
