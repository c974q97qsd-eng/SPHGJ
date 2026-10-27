# -*- coding: utf-8 -*-
"""rev47 patch 5: DOM 文本采样修空 —— 穿透 Shadow DOM + 空文本也要留证

现场实测(重启后 01:56~01:57):dom=None 且没有"直播页文本采样"日志,说明
document.body.innerText 拿到的是空串。原因:直播控制台是 wujie 微前端,
内容渲染在 Shadow DOM 里(与 rev25 发布页 wujie + Shadow DOM 同一个坑),
body.innerText 只反映 light DOM,自然为空。

修法:
  1. JS 递归遍历 shadowRoot(限深度 6),把各层 innerText 拼起来;
  2. 兜底 documentElement.innerText / textContent;
  3. 拿到空文本也打日志(以前是 if txt: 才打,于是"空"和"没跑"分不清);
  4. 空文本也落盘(写一个只有标记的文件),便于判断是页面没内容还是取文本失败。
"""
import io

P = r"D:\SPHGJ\backend\live_fetcher.py"

OLD_JS = '''        js = ("() => { const b = document.body; if (!b) return '';"
              " return (b.innerText || '').replace(/\\\\s+/g, ' ').slice(0, 2000); }")'''

NEW_JS = '''        # wujie 微前端把正文放在 Shadow DOM 里,body.innerText 是空的(实测),
        # 必须递归遍历 shadowRoot 才能取到。深度限 6、总量限 3000 字,开销可控。
        js = """() => {
            const out = [];
            const walk = (root, depth) => {
                if (!root || depth > 6 || out.length > 40) return;
                try {
                    const t = root.innerText || '';
                    if (t && t.trim()) out.push(t);
                } catch (e) {}
                let nodes = [];
                try { nodes = root.querySelectorAll ? root.querySelectorAll('*') : []; } catch (e) {}
                for (const el of nodes) {
                    if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
                }
            };
            walk(document, 0);
            if (!out.length) {
                try {
                    const d = document.documentElement;
                    if (d) out.push(d.innerText || d.textContent || '');
                } catch (e) {}
            }
            return out.join(' || ').replace(/\\s+/g, ' ').trim().slice(0, 3000);
        }"""'''

PATCHES = [
    (OLD_JS, NEW_JS, "shadow-js"),
    (
        '''        if dump and not self._dom_dumped and txt:
            self._dom_dumped = True
            try:
                _d = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_d, exist_ok=True)
                with io_open(os.path.join(_d, f"{self.account_id}_dom.txt"), "w") as f:
                    f.write(txt)
            except Exception:
                pass
            logger.info(f"[live:{self.account_id}] 直播页文本采样({len(txt)}字,"
                        f"frames={len(frames)}): 命中={matched or '无'} "
                        f"状态={'在播' if state is True else ('未播' if state is False else '未知')} "
                        f"| {txt[:180]}")''',
        '''        if dump and not self._dom_dumped:
            self._dom_dumped = True
            try:
                _d = os.path.join(os.path.dirname(_SPEC_CACHE_PATH), "live_probe")
                os.makedirs(_d, exist_ok=True)
                with io_open(os.path.join(_d, f"{self.account_id}_dom.txt"), "w") as f:
                    f.write(txt or "<EMPTY: 所有 frame 的文本都是空>")
            except Exception:
                pass
            # 空文本也要打:以前是 if txt 才打,于是"取到空"和"根本没跑"在日志里分不清
            logger.info(f"[live:{self.account_id}] 直播页文本采样({len(txt)}字,"
                        f"frames={len(frames)}): 命中={matched or '无'} "
                        f"状态={'在播' if state is True else ('未播' if state is False else '未知')} "
                        f"| {(txt or '<空>')[:200]}")''',
        "dump-log",
    ),
]

s = io.open(P, encoding="utf-8").read()
for old, new, tag in PATCHES:
    assert s.count(old) == 1, f"[{tag}] 命中 {s.count(old)} 次,应为 1 次"
    s = s.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)

s = io.open(P, encoding="utf-8").read()
checks = [
    ("shadowRoot 遍历", "el.shadowRoot" in s),
    ("documentElement 兜底", "document.documentElement" in s),
    ("空文本也落盘", "<EMPTY" in s),
    ("空文本也打日志", "(txt or '<空>')" in s),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK   " if ok else "  FAIL ") + n)
assert not bad, "自校验失败: " + ", ".join(bad)
print("\nSELFCHECK PASS dom shadow")
