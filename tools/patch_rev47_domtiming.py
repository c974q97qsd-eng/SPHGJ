# -*- coding: utf-8 -*-
"""rev47 patch 6: DOM 采样取到 CSS + 取样时机太早

实测(01:59~02:00):
  [live:uspoloassn2] 直播页文本采样(3000字,frames=1): 命中=无 状态=未知 |
  视频号助手 :root { --Finder-primary: #fa9d3b; ... }
两个问题:
  ① 回退用了 document.documentElement.textContent -> 把 <style> 里的 CSS 全吃进来,
     3000 字配额被 CSS 占满,真正的界面文案一个字没取到;
  ② 取样点在 goto_live() 之后【立刻】,而 goto 用的是 domcontentloaded,SPA 还没渲染。

修法:
  A. JS:克隆 body -> 删掉 style/script/noscript/template -> 用 textContent;
     再不行才走 shadowRoot 递归;彻底不要整份 documentElement 文本。
  B. 取样点:goto 后那次降级为"预热"(dump=False),【判定前】再采一次(dump=True),
     此时已经等过 20~32 秒,页面早已成型,这次才是判据要用的结果。
"""
import io

LF = r"D:\SPHGJ\backend\live_fetcher.py"
AM = r"D:\SPHGJ\backend\account_manager.py"

# ---------------- A. live_fetcher: 换 JS ----------------
s = io.open(LF, encoding="utf-8").read()
start = s.index('        # wujie 微前端把正文放在 Shadow DOM 里,body.innerText 是空的(实测),')
end = s.index('        for fr in frames:')
new_js = '''        # 取文本的三道处理(每一道都是踩过的坑):
        #   ① 克隆 body 并删掉 style/script/noscript —— 直接 textContent 会把 <style>
        #      里的 CSS 全吃进来(实测 3000 字配额被 CSS 占满,界面文案一个字没取到);
        #   ② innerText 优先(反映可见文本),空则 textContent;
        #   ③ 还空就递归 shadowRoot —— wujie 微前端(直播控制台/发布页)把正文放 Shadow DOM。
        js = r"""() => {
            const clean = (root) => {
                try {
                    const c = root.cloneNode(true);
                    if (c.querySelectorAll) {
                        c.querySelectorAll('style,script,noscript,template,link,meta').forEach(e => e.remove());
                    }
                    return c;
                } catch (e) { return root; }
            };
            const grab = (root) => {
                try {
                    const c = clean(root);
                    const t = c.innerText || c.textContent || '';
                    return t.replace(/\\s+/g, ' ').trim();
                } catch (e) { return ''; }
            };
            const parts = [];
            let t = grab(document.body || document.documentElement);
            if (t) parts.push(t);
            if (!parts.length) {
                const walk = (root, depth) => {
                    if (!root || depth > 6 || parts.length > 20) return;
                    const x = grab(root);
                    if (x) parts.push(x);
                    let nodes = [];
                    try { nodes = root.querySelectorAll ? root.querySelectorAll('*') : []; } catch (e) {}
                    for (const el of nodes) {
                        if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                    }
                };
                walk(document, 0);
            }
            return parts.join(' || ').slice(0, 3000);
        }"""
'''
s = s[:start] + new_js + s[end:]
io.open(LF, "w", encoding="utf-8", newline="").write(s)

# ---------------- B. account_manager: 取样时机 ----------------
s = io.open(AM, encoding="utf-8").read()
old_goto = '''            # rev47: 接口层没有"在播"布尔位(未开播时只有 status=0),所以页面文本
            # 是最廉价、最不依赖接口字段的兜底判据。只读文本,不点任何按钮。
            try:
                await lf.read_dom_state(dump=True)
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 读直播页文本失败: {e}")'''
new_goto = '''            # rev47: 接口层没有"在播"布尔位(未开播时只有 status=0),所以页面文本
            # 是最廉价、最不依赖接口字段的兜底判据。但这里刚 goto 完(domcontentloaded),
            # SPA 还没渲染,读到的多半是空 —— 只作预热,真正的采样在判定前做。
            try:
                await lf.read_dom_state(dump=False)
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 预热读直播页文本失败: {e}")'''
assert s.count(old_goto) == 1
s = s.replace(old_goto, new_goto, 1)

old_wait = '''            info = await self._wait_for_live_detection(lf, timeout=20.0)'''
new_wait = '''            info = await self._wait_for_live_detection(lf, timeout=20.0)
            # rev47: 判定前再采一次页面文本 —— 这时已经等过 20~32 秒,直播大屏页渲染完成,
            # 这次的结果才进判据;诊断 dump 也只在这里做(每轮检测一份)。
            try:
                await lf.read_dom_state(dump=True)
                info = await lf.snapshot()
            except Exception as e:
                logger.debug(f"[{self.account['id']}] 判定前读直播页文本失败: {e}")'''
assert s.count(old_wait) == 1
s = s.replace(old_wait, new_wait, 1)
io.open(AM, "w", encoding="utf-8", newline="").write(s)

# ---------------- 自校验 ----------------
s1 = io.open(LF, encoding="utf-8").read()
s2 = io.open(AM, encoding="utf-8").read()
checks = [
    ("JS 删 style/script", "style,script,noscript,template,link,meta" in s1),
    ("不再用 documentElement.textContent 兜底", "d.innerText || d.textContent" not in s1),
    ("shadow walk 仍在", "el.shadowRoot" in s1),
    ("预热采样 dump=False", "await lf.read_dom_state(dump=False)" in s2),
    ("判定前采样 dump=True", "await lf.read_dom_state(dump=True)" in s2),
    ("判定前刷新 snapshot", "info = await lf.snapshot()" in s2),
]
bad = [n for n, o in checks if not o]
for n, o in checks:
    print(("  OK   " if o else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS dom timing")
