"""视频号作品自动发布。

发布页 https://channels.weixin.qq.com/platform/post/create 是 wujie 微前端,
真正的控件挂在 Shadow DOM(可能还套 iframe)里,class 名是编译产物。
视频号没有公开的「上传/发表」接口,上传+转码走页面内私有分片通道,
复现成本高且易随前端版本变化 —— 所以发布只能走 UI 自动化。
好消息:Playwright 的定位器(CSS/locator)天然穿透 open shadow root,
比 CDP 的 DOM.querySelector 方便得多,set_input_files 也能直接注入隐藏的
input[type=file],不需要系统文件选择框。

本模块两部分:
  1. probe_post_create(): DOM 实测校准。选择器绝不能靠猜 —— 先用真实登录态
     把发布页的可交互元素、shadow root 层级、iframe 结构 dump 出来,再写死。
  2. VideoPublisher: 六步状态机 open -> upload -> edit -> fill -> submit -> verify
     每步独立超时;成功判据是回读作品列表真的出现这条,不是「按钮点了没」

幂等与安全:
  - 发表是写操作,风控比抓取敏感得多;调度层限制单号日限/随机间隔/时间窗
  - 成功判据不看「按钮点了没」,而是回读作品列表首屏是否真出现这条
"""
import asyncio
import json
import logging
import os
import time

logger = logging.getLogger("sphgj")

POST_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
POST_LIST_URL = "https://channels.weixin.qq.com/platform/post/list"

# DOM 探测用 JS:递归穿透 shadow root,只记录「可能有用」的元素
# (表单控件/可编辑区/带 role/class 命中关键词/shadow host),限 3000 条防爆内存。
_PROBE_JS = r"""() => {
  const out = {url: location.href, title: document.title, counts: {}, nodes: []};
  const clsOf = (el) => {
    try {
      const c = el.className;
      if (typeof c === 'string') return c;
      if (c && c.baseVal) return c.baseVal;
    } catch (e) {}
    return el.getAttribute ? (el.getAttribute('class') || '') : '';
  };
  const isVisible = (el) => {
    try {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return false;
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
    } catch (e) { return false; }
  };
  const KEY = /upload|add-icon|publish|submit|post|btn|editor|desc|title|cover|content|input|video/i;
  const walk = (root, path, depth) => {
    if (depth > 14 || out.nodes.length >= 3000) return;
    let els;
    try { els = root.querySelectorAll('*'); } catch (e) { return; }
    let i = 0;
    for (const el of els) {
      i++;
      const tag = el.tagName.toLowerCase();
      out.counts[tag] = (out.counts[tag] || 0) + 1;
      const cls = clsOf(el);
      const keep = (
        tag === 'input' || tag === 'textarea' || tag === 'button' ||
        tag === 'select' || tag === 'video' || tag === 'a' || tag === 'img' ||
        el.hasAttribute('contenteditable') || el.hasAttribute('role') ||
        el.hasAttribute('placeholder') || el.hasAttribute('aria-label') ||
        el.shadowRoot || KEY.test(cls)
      );
      if (keep && out.nodes.length < 3000) {
        out.nodes.push({
          p: path + '/' + tag + '[' + i + ']',
          tag: tag,
          id: el.id || '',
          cls: String(cls).slice(0, 140),
          type: el.getAttribute('type') || '',
          ph: (el.getAttribute('placeholder') || '').slice(0, 60),
          ce: el.getAttribute('contenteditable') || '',
          role: el.getAttribute('role') || '',
          aria: (el.getAttribute('aria-label') || '').slice(0, 60),
          txt: (el.innerText || el.textContent || '').trim().slice(0, 50),
          vis: isVisible(el),
          dis: el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true',
          shadow: !!el.shadowRoot,
          accept: (el.getAttribute('accept') || '').slice(0, 60)
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot, path + '/' + tag + '#' + i + '::shadow', depth + 1);
    }
  };
  walk(document, '', 0);
  return out;
}"""

# 点击候选上传入口后再探测(有些版本要点了「上传视频」才插入 input[type=file])
_CLICK_CANDIDATES_JS = r"""() => {
  const hits = [];
  const pick = (root) => {
    let els;
    try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      const cls = (typeof el.className === 'string' ? el.className : '') || '';
      const txt = (el.innerText || '').trim();
      if (/upload|add-icon|upload-content/i.test(cls) ||
          /^(点击上传|上传视频|上传作品|添加视频|发表视频|发布视频|上传)$/.test(txt)) {
        el.setAttribute('data-sphprobe', String(hits.length));
        hits.push({idx: hits.length, tag: el.tagName.toLowerCase(),
                   cls: cls.slice(0, 80), txt: txt.slice(0, 30)});
      }
      if (el.shadowRoot) pick(el.shadowRoot);
    }
  };
  pick(document);
  return hits;
}"""


async def _probe_frame(frame):
    try:
        return await asyncio.wait_for(frame.evaluate(_PROBE_JS), timeout=20)
    except Exception as e:
        return {"url": getattr(frame, "url", ""), "err": str(e)[:200], "nodes": []}


# 上传后的状态判据(以页面真实文案为准,class 名只作辅助)
_UPLOAD_DONE_HINTS = ("上传成功", "上传完成", "转码完成", "重新上传", "替换")
_UPLOAD_FAIL_HINTS = ("上传失败", "转码失败", "视频格式", "不支持")

# 探测时往描述框里写的自检文本(写完只回读验证,绝不点发表)
DESC_SAMPLE = "自动化发布链路自检 请忽略 #测试"

# 穿透 shadow root 的元素查找(原生 querySelector 看不到 shadow DOM 里的东西)
_DEEP_FIND_JS = r"""() => {
  const deep = (sel) => {
    const walk = (root) => {
      let hit = null;
      try { hit = root.querySelector(sel); } catch (e) {}
      if (hit) return hit;
      let els = [];
      try { els = root.querySelectorAll('*'); } catch (e) {}
      for (const el of els) {
        if (el.shadowRoot) {
          const r = walk(el.shadowRoot);
          if (r) return r;
        }
      }
      return null;
    };
    return walk(document);
  };
  const snap = (sel) => {
    const el = deep(sel);
    if (!el) return null;
    return {sel: sel, tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' ? el.className : ''),
            ce: el.getAttribute('contenteditable'),
            html: (el.outerHTML || '').slice(0, 2200),
            text: (el.innerText || '').slice(0, 300)};
  };
  const out = {nodes: {}, ce_count: 0, ce_list: []};
  for (const s of ['div.post-desc-box', 'div.input-editor',
                   'div.post-desc-box div.input-editor']) {
    out.nodes[s] = snap(s);
  }
  // 全文档 contenteditable 清点(含 shadow)
  const countCe = (root) => {
    let els = [];
    try { els = root.querySelectorAll('[contenteditable]'); } catch (e) {}
    for (const el of els) {
      if (out.ce_list.length < 12) {
        out.ce_list.push({tag: el.tagName.toLowerCase(),
          cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80),
          ce: el.getAttribute('contenteditable'),
          ph: el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
          vis: !!(el.getBoundingClientRect().width && el.getBoundingClientRect().height)});
      }
    }
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) {}
    for (const el of all) {
      if (el.shadowRoot) countCe(el.shadowRoot);
    }
  };
  countCe(document);
  out.ce_count = out.ce_list.length;
  return out;
}"""


async def _desc_probe(page, result, sample_text=""):
    """点开描述编辑器,探测真实可编辑节点,并(可选)试写文本后回读。

    只写不提交:写完读回 innerText/value 验证,页面随后整体关闭,不点发表。
    """
    info = {"steps": []}
    try:
        box = page.locator("div.post-desc-box").first
        info["box_count"] = await page.locator("div.post-desc-box").count()
        await box.click(timeout=8000)
        await page.wait_for_timeout(1000)
        info["steps"].append("clicked post-desc-box")
    except Exception as e:
        info["click_err"] = f"{type(e).__name__}: {str(e)[:200]}"

    # 候选选择器逐个清点可见性
    cands = ["div.post-desc-box .input-editor", "div.input-editor",
             "div.post-desc-box [contenteditable='true']", "[contenteditable='true']",
             ".post-desc-box textarea", "textarea",
             "div.post-desc-box div[role='textbox']", "[role='textbox']"]
    info["cands"] = {}
    for sel in cands:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            vis = []
            for i in range(min(n, 3)):
                try:
                    vis.append(await loc.nth(i).is_visible())
                except Exception:
                    vis.append(None)
            info["cands"][sel] = {"count": n, "visible": vis}
        except Exception as e:
            info["cands"][sel] = {"err": str(e)[:80]}

    try:
        info["structure"] = await page.evaluate(_DEEP_FIND_JS)
        info["steps"].append("dumped structure")
    except Exception as e:
        info["structure_err"] = str(e)[:200]

    # 试写:优先 contenteditable / textbox,退回 input-editor 后键盘输入
    if sample_text:
        for sel in ("div.post-desc-box [contenteditable='true']", "[contenteditable='true']",
                    "div.input-editor", "div.post-desc-box"):
            try:
                loc = page.locator(sel).first
                if not await loc.count():
                    continue
                await loc.click(timeout=6000)
                await page.wait_for_timeout(300)
                try:
                    await loc.fill(sample_text, timeout=6000)
                    how = "fill"
                except Exception:
                    await page.keyboard.type(sample_text, delay=30)
                    how = "type"
                await page.wait_for_timeout(600)
                got = ""
                try:
                    got = await page.evaluate("""() => {
                      const deep = (sel) => {
                        const walk = (root) => {
                          let h = null;
                          try { h = root.querySelector(sel); } catch (e) {}
                          if (h) return h;
                          let els = [];
                          try { els = root.querySelectorAll('*'); } catch (e) {}
                          for (const el of els) {
                            if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; }
                          }
                          return null;
                        };
                        return walk(document);
                      };
                      const el = deep('div.post-desc-box');
                      return el ? (el.innerText || '').slice(0, 200) : '';
                    }""")
                except Exception:
                    pass
                info["write"] = {"sel": sel, "how": how, "readback": got,
                                 "ok": bool(sample_text[:8] in (got or ""))}
                info["steps"].append(f"write via {sel} ({how}) -> ok={info['write']['ok']}")
                if info["write"]["ok"]:
                    break
            except Exception as e:
                info.setdefault("write_errors", []).append(f"{sel}: {type(e).__name__}: {str(e)[:120]}")
    result["desc_probe"] = info


async def _dump_phases(page, result, name):
    frames = []
    for fr in page.frames:
        info = await _probe_frame(fr)
        info["frame_name"] = fr.name or ""
        info["frame_url"] = fr.url
        info["is_main"] = (fr == page.main_frame)
        frames.append(info)
    result["phases"].append({"name": name, "frames": frames})
    return frames


async def probe_post_create(ctx, out_dir, wait_ms=9000, upload_file=""):
    """用给定 context 打开发布页,dump DOM 结构到 out_dir/publish_dom_probe.json。

    只读取页面,不填写、不提交、对账号零副作用。
    upload_file 给定时:注入一个测试视频并等它上传/转码(仍不发表),
    用来探测「上传后才渲染」的描述编辑器 —— 未上传时页面上没有描述框。
    探测结束直接关页面,不点「保存草稿」也不点「发表」。
    """
    page = await ctx.new_page()
    result = {"probed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "phases": []}
    try:
        await page.goto(POST_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
        # wujie 微前端初始化 + 登录态校验需要时间
        await page.wait_for_timeout(wait_ms)
        result["final_url"] = page.url
        result["logged_in"] = "login" not in (page.url or "")

        # ---------- Phase A: 初始结构 ----------
        frames = await _dump_phases(page, result, "initial")

        # ---------- Phase B: 无 file input 时,尝试点上传入口后再探测 ----------
        has_file_input = any(
            n.get("tag") == "input" and n.get("type") == "file"
            for fr in frames for n in (fr.get("nodes") or [])
        )
        result["has_file_input_initial"] = has_file_input
        if not has_file_input:
            clicked = []
            try:
                cands = await page.evaluate(_CLICK_CANDIDATES_JS)
            except Exception as e:
                cands = []
                result["click_err"] = str(e)[:200]
            for c in (cands or [])[:3]:
                try:
                    loc = page.locator(f'[data-sphprobe="{c["idx"]}"]').first
                    await loc.click(timeout=5000)
                    await page.wait_for_timeout(2500)
                    clicked.append(c)
                    snap = await _probe_frame(page.main_frame)
                    result["phases"].append({
                        "name": f"after_click_{c['idx']}", "clicked": c,
                        "frames": [snap]})
                    if any(n.get("tag") == "input" and n.get("type") == "file"
                           for n in (snap.get("nodes") or [])):
                        break
                except Exception as e:
                    clicked.append({**c, "click_failed": str(e)[:160]})
            result["clicked"] = clicked

        # ---------- Phase C: 注入测试视频,探测「上传后才有」的控件 ----------
        if upload_file:
            result["upload_file"] = upload_file
            if not os.path.isfile(upload_file):
                result["upload_err"] = "文件不存在"
            else:
                try:
                    fi = page.locator('input[type="file"]').first
                    await fi.set_input_files(upload_file, timeout=20000)
                    result["upload_injected"] = True
                except Exception as e:
                    result["upload_injected"] = False
                    result["upload_err"] = f"{type(e).__name__}: {str(e)[:200]}"
                if result.get("upload_injected"):
                    # 结构化判据:原生 innerText 取不到 shadow DOM 文本,必须用 locator
                    desc_ready = False
                    for i in range(30):
                        await page.wait_for_timeout(5000)
                        st = {"i": i}
                        try:
                            st["desc_box"] = await page.locator("div.post-desc-box").first.is_visible()
                        except Exception:
                            st["desc_box"] = None
                        try:
                            st["upload_wrap"] = await page.locator("div.upload-wrap").first.is_visible()
                        except Exception:
                            st["upload_wrap"] = None
                        try:
                            pub_btn = page.locator("div.form-btns button", has_text="发表").first
                            cls = (await pub_btn.get_attribute("class") or "")
                            st["pub_btn"] = {"found": True, "disabled": "_disabled" in cls}
                        except Exception as e:
                            st["pub_btn"] = {"found": False, "err": str(e)[:60]}
                        result.setdefault("upload_poll", []).append(st)
                        if st.get("desc_box") and not desc_ready:
                            desc_ready = True
                            await _dump_phases(page, result, f"uploaded_{i}")
                            await _desc_probe(page, result, sample_text=DESC_SAMPLE)
                        if desc_ready and (st.get("pub_btn") or {}).get("disabled") is False:
                            result["publish_ready_at"] = i
                            break
    except Exception as e:
        result["fatal"] = str(e)[:400]
        logger.warning(f"[publish] 探测发布页失败: {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "publish_dom_probe.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

    # 摘要:只挑「大概率是选择器目标」的元素,方便一眼看出结论(跨 phase 去重)
    key_nodes, seen = [], set()
    for ph in result["phases"]:
        for fr in ph.get("frames") or []:
            if not fr.get("is_main"):
                continue  # iframe 是同一份 DOM 的另一视图,只看主 frame 免重复
            for n in fr.get("nodes") or []:
                t = n.get("type") or ""
                txt = (n.get("txt") or "").strip()
                if not (t in ("file", "text", "checkbox", "radio")
                        or n.get("ce") in ("true", "plaintext-only")
                        or n.get("tag") == "textarea"
                        or (n.get("tag") == "button" and 0 < len(txt) < 14)
                        or "上传" in txt or "发表" in txt or "发布" in txt):
                    continue
                sig = (n.get("tag"), t, n.get("ph"), n.get("ce"), n.get("cls"), n.get("txt"))
                if sig in seen:
                    continue
                seen.add(sig)
                key_nodes.append({"phase": ph.get("name"), **{k: n[k] for k in
                    ("p", "tag", "type", "ph", "ce", "cls", "txt", "vis", "dis", "accept")
                    if k in n}})
    result["summary"] = key_nodes[:80]
    logger.info(f"[publish] DOM 探测完成 -> {path} (关键元素 {len(key_nodes)} 个)")
    return {"ok": "fatal" not in result, "file": path,
            "final_url": result.get("final_url"), "logged_in": result.get("logged_in"),
            "has_file_input": result.get("has_file_input_initial"),
            "upload_file": result.get("upload_file"),
            "upload_injected": result.get("upload_injected"),
            "upload_err": result.get("upload_err"),
            "upload_poll": (result.get("upload_poll") or [])[-8:],
            "publish_ready_at": result.get("publish_ready_at"),
            "desc_probe": result.get("desc_probe"),
            "key_nodes": result["summary"]}

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
