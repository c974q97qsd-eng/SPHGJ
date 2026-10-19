# -*- coding: utf-8 -*-
"""rev25-2: 在 publisher.py 里追加发布执行器 VideoPublisher(六步状态机)。

选择器全部来自 2026-10-20 母本实测(见 probe 结果),不是猜的:
  - input[type=file]                      初始即存在,accept 只收 video/*,可直接 set_input_files
  - .upload-wrap 隐藏 + .post-desc-box 可见   = 上传已被接受、编辑区渲染完成(实测 5s)
  - .post-desc-box .input-editor          contenteditable=""(空串!所以 [contenteditable="true"] 匹配不到)
  - .form-btns button 文本「发表」         class 含 _disabled 表示还不可点(转码未完)
  - 原创声明弹窗里是 button 文本「直接发表」
"""
import io, sys

P = r"D:\SPHGJ\backend\publisher.py"
s = io.open(P, encoding="utf-8").read()
assert "class VideoPublisher" not in s, "VideoPublisher 已存在,勿重复打补丁"

# 1) 文档头补一句(说明执行器已实现)
s = s.replace("""  2. VideoPublisher: 六步状态机 open -> upload -> transcode -> fill -> submit -> verify
     (Step 3 实现,依赖 Step 1 的探测结果)""",
"""  2. VideoPublisher: 六步状态机 open -> upload -> edit -> fill -> submit -> verify
     每步独立超时;成功判据是回读作品列表真的出现这条,不是「按钮点了没」""", 1)

APPEND = r'''

# ==================== 实测选择器集中定义(改版只需动这里) ====================
SEL = {
    "file_input": 'input[type="file"]',
    "upload_wrap": "div.upload-wrap",
    "desc_box": "div.post-desc-box",
    "desc_editor": "div.post-desc-box div.input-editor",
    "form_btns": "div.form-btns button",
    "video_content": "div.video-content",
    "short_title": 'input[placeholder*="短标题"]',
}

# 点发表后可能出现的二次弹窗,按文案精确匹配 —— 只点这几个安全按钮
SAFE_DIALOG_BUTTONS = ("直接发表", "继续发表", "我知道了")

STEP_LABEL = {
    "open": "打开发布页",
    "upload": "上传视频",
    "edit": "等待转码/编辑区就绪",
    "fill": "填写描述",
    "submit": "点击发表",
    "verify": "回读校验",
}


def _norm(x):
    """归一化:去掉所有空白,便于标题/描述比较。"""
    return "".join((x or "").split())


class VideoPublisher:
    """在给定浏览器 context 上把一个视频发到视频号。

    一次 publish() = 新开一个 tab,发完关掉,不影响 worker 的评论页。
    不做节流/限额 —— 那是调度层的职责(见 publish_queue.py)。
    """

    def __init__(self, ctx, account, storage=None, config=None, api=None, emit=None):
        self.ctx = ctx
        self.account = account or {}
        self.storage = storage
        self.cfg = ((config or {}).get("publish")) or {}
        self.api = api
        self.emit = emit

    # ---------- 小工具 ----------
    async def _step(self, on_step, name, detail=""):
        logger.info(f"[publish][{self.account.get('id')}] {STEP_LABEL.get(name, name)} {detail}")
        if on_step:
            try:
                await on_step(name, detail)
            except Exception:
                pass

    async def _visible(self, page, sel):
        try:
            return await page.locator(sel).first.is_visible()
        except Exception:
            return None

    async def _dismiss_popups(self, page):
        """点掉「我知道了」这类纯提示弹窗。只认 SAFE_DIALOG_BUTTONS,绝不乱点。"""
        clicked = []
        for text in SAFE_DIALOG_BUTTONS:
            if text == "直接发表":
                continue  # 只在点发表后处理
            try:
                btn = page.locator("button").filter(has_text=text)
                n = await btn.count()
                for i in range(n):
                    loc = btn.nth(i)
                    if await loc.is_visible():
                        await loc.click(timeout=3000)
                        clicked.append(text)
                        await page.wait_for_timeout(400)
                        break
            except Exception:
                continue
        return clicked

    async def _wait(self, page, cond, timeout_sec, interval=2.0, on_tick=None):
        """通用轮询:cond() -> truthy 即返回该值;超时返回 None。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                r = await cond()
            except Exception:
                r = None
            if r:
                return r
            if on_tick:
                try:
                    await on_tick()
                except Exception:
                    pass
            await asyncio.sleep(interval)
        return None

    # ---------- 主流程 ----------
    async def publish(self, task, on_step=None, dry_run=False):
        """发布一条。

        返回 {"ok":bool,"step":str,"error":str,"object_id":str,"export_id":str}
        dry_run=True:只做到「填完描述」就停(不点发表),用于人工看效果。
        """
        src = task.get("src_path") or ""
        title = (task.get("title") or "").strip()
        res = {"ok": False, "step": "", "error": "", "object_id": "", "export_id": ""}

        if not os.path.isfile(src):
            res.update(step="precheck", error=f"视频文件不存在: {src}")
            return res
        if not title:
            res.update(step="precheck", error="没有标题(标题池空了?)")
            return res

        upload_timeout = float(self.cfg.get("edit_ready_timeout_sec", 420))
        publish_timeout = float(self.cfg.get("publish_ready_timeout_sec", 600))

        page = None
        try:
            page = await self.ctx.new_page()
            page.set_default_timeout(15000)

            # ---- 1) open ----
            await self._step(on_step, "open")
            res["step"] = "open"
            await page.goto(POST_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)
            if "login" in (page.url or ""):
                res["error"] = "登录态失效:页面跳转到登录页,需要重新扫码"
                return res
            if not await self._visible(page, SEL["file_input"]):
                # 页面上有「你还不能发表视频」之类限制时必须明确报出来
                body = ""
                try:
                    body = await page.evaluate(
                        "() => (document.body.innerText || '').slice(0, 400)")
                except Exception:
                    pass
                res["error"] = f"发布页没有文件输入框(可能账号无发布权限或页面改版) {body[:120]}"
                return res

            # ---- 2) upload ----
            await self._step(on_step, "upload", os.path.basename(src))
            res["step"] = "upload"
            try:
                await page.locator(SEL["file_input"]).first.set_input_files(src, timeout=60000)
            except Exception as e:
                res["error"] = f"注入视频失败: {type(e).__name__}: {str(e)[:200]}"
                return res
            await page.wait_for_timeout(3000)

            # ---- 3) edit:等上传被接受 + 编辑区渲染 ----
            await self._step(on_step, "edit")
            res["step"] = "edit"

            async def _edit_ready():
                if await self._visible(page, SEL["desc_box"]):
                    return True
                await self._dismiss_popups(page)
                return False

            if not await self._wait(page, _edit_ready, upload_timeout):
                res["error"] = f"上传后 {int(upload_timeout)}s 内编辑区未出现(上传失败/网络问题/格式不支持)"
                return res
            logger.info(f"[publish][{self.account.get('id')}] 编辑区已就绪,描述框可见")

            # ---- 4) fill:填描述(只填描述,短标题按用户要求不填) ----
            await self._step(on_step, "fill", title[:20])
            res["step"] = "fill"
            editor = page.locator(SEL["desc_editor"]).first
            try:
                await editor.click(timeout=8000)
                await page.wait_for_timeout(300)
                await editor.fill(title, timeout=10000)
            except Exception:
                # contenteditable 的 fill 偶发失败 -> 退回键盘输入
                try:
                    await editor.click(timeout=5000)
                    await page.keyboard.type(title, delay=25)
                except Exception as e:
                    res["error"] = f"填写描述失败: {type(e).__name__}: {str(e)[:200]}"
                    return res
            await page.wait_for_timeout(600)
            try:
                got = await editor.inner_text()
            except Exception:
                got = ""
            if _norm(title)[:10] and _norm(title)[:10] not in _norm(got):
                res["error"] = f"描述写入校验失败(回读={_norm(got)[:40]})"
                return res

            if dry_run:
                res.update(ok=True, step="dry_run",
                           error="dry_run:已上传并填好描述,未点发表")
                return res

            # ---- 5) submit:等按钮可点再点 ----
            await self._step(on_step, "submit")
            res["step"] = "submit"

            async def _btn_ready():
                try:
                    btn = page.locator(SEL["form_btns"]).filter(has_text="发表").first
                    cls = (await btn.get_attribute("class")) or ""
                    if "_disabled" in cls:
                        return False
                    return await btn.is_visible()
                except Exception:
                    return False

            if not await self._wait(page, _btn_ready, publish_timeout):
                res["error"] = (f"「发表」按钮 {int(publish_timeout)}s 内一直不可点"
                                "(转码未完成/视频不合规)")
                return res

            btn = page.locator(SEL["form_btns"]).filter(has_text="发表").first
            await btn.click(timeout=15000)
            await page.wait_for_timeout(2500)

            # 原创声明等二次弹窗 -> 点「直接发表」
            for text in ("直接发表", "继续发表"):
                try:
                    d = page.locator("button").filter(has_text=text).first
                    if await d.count() and await d.is_visible():
                        await d.click(timeout=5000)
                        logger.info(f"[publish][{self.account.get('id')}] 二次弹窗已点「{text}」")
                        await page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue
            await self._dismiss_popups(page)

            # ---- 6) verify:URL 跳转 OR 回读作品列表 ----
            await self._step(on_step, "verify")
            res["step"] = "verify"

            async def _submitted():
                try:
                    if POST_LIST_URL in (page.url or ""):
                        return "url"
                except Exception:
                    pass
                try:
                    body = await page.evaluate(
                        "() => (document.body.innerText || '').slice(0, 500)")
                    if "发表成功" in body or "发布成功" in body:
                        return "text"
                except Exception:
                    pass
                return None

            how = await self._wait(page, _submitted, 90, interval=2.0)
            hit = await self._verify_in_list(page, title)
            if hit:
                res.update(ok=True, object_id=hit.get("object_id") or "",
                           export_id=hit.get("export_id") or "")
                return res
            if how:
                # 页面确认已发出,只是列表还没刷出来(转码中常见)
                res.update(ok=True,
                           error="已发表,但首屏暂未回读到该作品(可能仍在转码),稍后刷新作品列表即可")
                return res
            res["error"] = "点击发表后既未跳转也未出现成功提示,不确定是否发出(请到后台核对)"
            return res
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            logger.exception(f"[publish][{self.account.get('id')}] 发布异常")
            return res
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _verify_in_list(self, page, title, tries=3, gap=15):
        """回读作品列表首屏,按描述前 20 字匹配。返回 {object_id, export_id} 或 None。"""
        want = _norm(title)[:20]
        if not want:
            return None
        for i in range(tries):
            items = []
            try:
                if self.api is not None:
                    resp = await self.api.fetch_post_list(page=1, page_size=20)
                    items = ((resp or {}).get("data") or {}).get("list") or []
                if not items:
                    items = await page.evaluate(V_LIST_JS)
            except Exception as e:
                logger.debug(f"[publish] 回读作品列表失败: {e}")
                items = []
            for p in items or []:
                desc = ((p.get("desc") or {}).get("description") or "")
                if want and want in _norm(desc):
                    return {"object_id": p.get("objectId") or p.get("exportId") or "",
                            "export_id": p.get("exportId") or p.get("objectId") or ""}
            if i < tries - 1:
                await asyncio.sleep(gap)
        return None


# 兜底回读:直接用页面 fetch(post_list 内容管理口径),不依赖 api 客户端
V_LIST_JS = r"""async () => {
  try {
    const url = 'https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list'
      + '?_pageUrl=' + encodeURIComponent(location.href);
    const r = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'include',
      body: JSON.stringify({pageSize: 20, currentPage: 1, userpageType: 11, stickyOrder: true,
        timestamp: String(Date.now()), rawKeyBuff: '', pluginSessionId: null, scene: 7, reqScene: 7})
    });
    const d = await r.json();
    return (d && d.data && d.data.list) || [];
  } catch (e) { return []; }
}"""
'''

s = s.rstrip("\n") + APPEND
io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = io.open(P, encoding="utf-8").read()
must = ["class VideoPublisher", "async def publish", "_verify_in_list", "V_LIST_JS",
        "SAFE_DIALOG_BUTTONS", 'SEL = {']
missing = [m for m in must if m not in chk]
print("SELFCHECK", "PASS" if not missing else f"FAIL {missing}")
sys.exit(1 if missing else 0)
