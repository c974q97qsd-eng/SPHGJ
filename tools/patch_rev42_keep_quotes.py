# -*- coding: utf-8 -*-
"""rev42: 引号保留(用户 2026-10-28 定),其余规则不变

用户原话:「引号留着 其他就这样了」
-> 去掉 rev41 里的引号剥离逻辑,`"标题"` / “标题” 原样保留。
   行首序号、行首标签、零宽字符、全角空格、连续空格合一、整段一行切分 全部保留。
"""
import io

PATCHES = [
    # ---------------- backend/title_clean.py: 文档说明 ----------------
    (
        r"D:\SPHGJ\backend\title_clean.py",
        '''设计原则:**只清格式,不动文案**。话题(#xxx)、中文标点、emoji、英文全部原样保留。
"""''',
        '''设计原则:**只清格式,不动文案**。话题(#xxx)、中文标点、emoji、英文全部原样保留。

⚠️ 引号(rev42,用户 2026-10-28 明确要求):`"标题"` `“标题”` `「标题」` **一律保留**,
   不做剥离 —— 别"顺手"加回来。行末尾同样只去空格,标点一个不剥。
"""''',
        "doc",
    ),
    # ---------------- backend/title_clean.py: 删引号常量 ----------------
    (
        r"D:\SPHGJ\backend\title_clean.py",
        '''# 整条被引号包起来
_QUOTE_PAIRS = (('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"), ("「", "」"), ("『", "』"))
# 行内序号''',
        '''# 行内序号''',
        "const",
    ),
    # ---------------- backend/title_clean.py: 删 _strip_quotes ----------------
    (
        r"D:\SPHGJ\backend\title_clean.py",
        '''def _strip_quotes(s):
    """去掉引号:整条被包住的、以及「开头引号…中间闭合」后面还跟话题的。

    例:  “质感绝了！中帮老钱风休闲鞋” #软底休闲男鞋
      ->  质感绝了！中帮老钱风休闲鞋 #软底休闲男鞋
    """
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for lq, rq in _QUOTE_PAIRS:
            if s.startswith(lq):
                i = s.find(rq, 1)
                if i > 0:
                    s = re.sub(r"\\s+", " ", (s[1:i] + " " + s[i + 1:])).strip()
                    changed = True
                    break
            if s.endswith(rq):
                j = s.rfind(lq, 0, len(s) - 1)
                if j >= 0:
                    s = re.sub(r"\\s+", " ", (s[:j] + " " + s[j + 1:-1])).strip()
                    changed = True
                    break
    return s


def _clean_one(line):''',
        '''def _clean_one(line):''',
        "func",
    ),
    # ---------------- backend/title_clean.py: 不再调引号剥离 ----------------
    (
        r"D:\SPHGJ\backend\title_clean.py",
        '''    # 序号 / 标签 / 引号可能互相嵌套(「1. "标题"」),循环两遍足够
    for _ in range(2):
        s = _RE_LINE_NO.sub("", s, count=1).strip()
        s = _RE_LABEL.sub("", s, count=1).strip()
        s = _strip_quotes(s)''',
        '''    # 序号 / 标签可能互相嵌套(「示例:1. 标题」),循环两遍足够。
    # 引号不处理(rev42 用户要求保留)
    for _ in range(2):
        s = _RE_LINE_NO.sub("", s, count=1).strip()
        s = _RE_LABEL.sub("", s, count=1).strip()''',
        "call",
    ),
    # ---------------- 前端文案:不再提引号 ----------------
    (
        r"D:\SPHGJ\frontend\src\lib\api.ts",
        '''  /** 粘贴预览:清理掉行首序号/引号/全角空格后的标题列表(不落库) */''',
        '''  /** 粘贴预览:清理掉行首序号/全角空格后的标题列表(引号保留,不落库) */''',
        "api-comment",
    ),
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''                {preview.cleaned > 0 && <span className="text-muted-foreground">· 已自动清理 {preview.cleaned} 条的序号/引号/全角空格</span>}''',
        '''                {preview.cleaned > 0 && <span className="text-muted-foreground">· 已自动清理 {preview.cleaned} 条的序号/全角空格</span>}''',
        "panel-preview",
    ),
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''      if (r.cleaned) bits.push(`已自动清理 ${r.cleaned} 条的序号/引号等格式`)''',
        '''      if (r.cleaned) bits.push(`已自动清理 ${r.cleaned} 条的序号等格式`)''',
        "panel-toast",
    ),
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''              placeholder={"直接粘贴即可,行首序号、引号、全角空格会自动清掉。例如:\\n1. 秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\\n2. 这双鞋的配色比图片还好看 #穿搭"}''',
        '''              placeholder={"直接粘贴即可,行首序号、全角空格会自动清掉,引号和行尾标点保留。例如:\\n1. 秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\\n2. 这双鞋的配色比图片还好看 #穿搭"}''',
        "panel-placeholder",
    ),
    # ---------------- 自测:加引号保留断言 ----------------
    (
        r"D:\SPHGJ\tools\test_title_clean.py",
        '''print("\\nASSERT OK: 示范文本两种形态都是 9 条,无行首序号")''',
        '''# rev42: 引号必须原样保留
q = split_titles('1. "质感绝了！中帮老钱风休闲鞋" #软底休闲男鞋')[0]
assert q == ['"质感绝了！中帮老钱风休闲鞋" #软底休闲男鞋'], q
q2 = split_titles("1. “老钱风精髓” #软底男鞋")[0]
assert q2 == ["“老钱风精髓” #软底男鞋"], q2
# 行尾标点一个不剥
t3 = split_titles("1. 中帮老钱风｜")[0]
assert t3 == ["中帮老钱风｜"], t3
print("\\nASSERT OK: 示范文本两种形态都是 9 条,无行首序号;引号与行尾标点保留")''',
        "test",
    ),
]

SELFCHECK = [
    (r"D:\SPHGJ\backend\title_clean.py",
     ["引号(rev42", "引号不处理"], ["_strip_quotes", "_QUOTE_PAIRS"]),
    (r"D:\SPHGJ\frontend\src\lib\api.ts", ["引号保留"], []),
    (r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
     ["序号/全角空格", "引号和行尾标点保留"], ["序号/引号"]),
]

ok = True
for path, old, new, tag in PATCHES:
    s = io.open(path, encoding="utf-8").read()
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次,应为 1 -> {path}")
        ok = False
        continue
    io.open(path, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
    print(f"[ok] {tag}")

print("\n--- 自校验 ---")
for path, must, mustnot in SELFCHECK:
    s = io.open(path, encoding="utf-8").read()
    for k in must:
        good = s.count(k) >= 1
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 含 {k}")
        ok &= good
    for k in mustnot:
        good = s.count(k) == 0
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 已无 {k}")
        ok &= good

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
