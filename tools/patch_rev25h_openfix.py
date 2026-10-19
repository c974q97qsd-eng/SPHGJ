# -*- coding: utf-8 -*-
"""rev25-fix1: 修发布页 open 步的误判。

问题:发布页的 input[type=file] 是【隐藏】元素(实测 vis=false),
用 is_visible() 判断必然为 false -> 误报「没有文件输入框」直接失败。
另外 wujie 微前端渲染需要时间,固定等 6s 不够(探测时等了 9s 才稳)。

改法:改成「轮询 count() > 0」等待最多 40s,并区分「登录页」与「无发布权限」。
"""
import io, sys

P = r"D:\SPHGJ\backend\publisher.py"
s = io.open(P, encoding="utf-8").read()

OLD = '''            await page.goto(POST_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
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
                return res'''

NEW = '''            await page.goto(POST_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
            if "login" in (page.url or ""):
                res["error"] = "登录态失效:页面跳转到登录页,需要重新扫码"
                return res

            # 注意:input[type=file] 是【隐藏】元素(实测 vis=false),
            # 只能按存在性判断,不能用 is_visible;wujie 微前端渲染也需要时间,要轮询等。
            async def _file_input_ready():
                try:
                    return await page.locator(SEL["file_input"]).count() > 0
                except Exception:
                    return False

            if not await self._wait(page, _file_input_ready, 40, interval=2.0):
                body = ""
                try:
                    body = await page.evaluate(
                        "() => (document.body.innerText || '').slice(0, 400)")
                except Exception:
                    pass
                if "你还不能发表视频" in body:
                    res["error"] = f"该视频号没有发表视频的权限: {body[:120]}"
                elif "login" in (page.url or ""):
                    res["error"] = "登录态失效:页面跳转到登录页,需要重新扫码"
                else:
                    res["error"] = f"发布页 40s 内未渲染出上传控件(页面改版或网络问题) {body[:120]}"
                return res'''

assert s.count(OLD) == 1, "open 步锚点不唯一"
s = s.replace(OLD, NEW, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
chk = io.open(P, encoding="utf-8").read()
miss = [m for m in ("_file_input_ready", "轮询等", "没有发表视频的权限") if m not in chk]
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
