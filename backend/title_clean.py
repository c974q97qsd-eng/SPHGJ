# -*- coding: utf-8 -*-
"""标题池粘贴文本 -> 干净标题列表(rev41)

用户从聊天记录 / 表格 / 文档里复制一批标题时,夹带的「格式垃圾」有两类:

  1) 行首序号:`1.` `2、` `(3)` `【4】` `①` `一、` `-` `•`  —— 复制编号列表必带
  2) 隐形字符:全角空格、NBSP、零宽字符、行首尾空白、连续空格

还有两种粘贴形态必须兼容:
  - 正常多行(一行一条)
  - **整段粘成一行**,序号夹在中间:`1. xxx 2. xxx 3. xxx`
    (从聊天框复制常是这样;不切就会变成一条巨长的标题)

设计原则:**只清格式,不动文案**。话题(#xxx)、中文标点、emoji、英文全部原样保留。
"""
import re

_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
# 行首序号:1. / 1、 / 1) / (1) / 【1】 / [1] / 1: / ①-⑳ / 一、 / - • · *
# 分隔符后面紧跟数字的不算序号(避免把「1.5折」「3.5cm」切断)
_RE_LINE_NO = re.compile(
    r"^(?:"
    r"[\(（\[【]\s*\d{1,3}\s*[\)）\]】]"                    # (1) （1） 【1】 [1]
    r"|(?:\d{1,3}|[一二三四五六七八九十]{1,3})\s*[\.、\)）:：]\s*(?!\d)"  # 1. 1、1) 1: 一、
    r"|[\u2460-\u2473]"                                    # ① ② ⑳
    r"|[-\u2013\u2014\u2022\u00b7\*]{1,2}"                 # - • · *
    r")\s*"
)
# 行首标签:示例: / 范例: / 标题: / 文案: / 描述: / 第1条:
_RE_LABEL = re.compile(r"^(?:示例|范例|标题|文案|描述|第\s*\d{1,3}\s*[条个])\s*[:：]\s*")
# 整条被引号包起来
_QUOTE_PAIRS = (('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"), ("「", "」"), ("『", "』"))
# 行内序号(整段粘成一行时):空白后面跟 数字+.、) —— 零宽前瞻,便于 split
_RE_INLINE_NO = re.compile(r"(?<=\s)(?=\d{1,2}\s*[\.、\)）](?!\d))")


def _strip_quotes(s):
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
                    s = re.sub(r"\s+", " ", (s[1:i] + " " + s[i + 1:])).strip()
                    changed = True
                    break
            if s.endswith(rq):
                j = s.rfind(lq, 0, len(s) - 1)
                if j >= 0:
                    s = re.sub(r"\s+", " ", (s[:j] + " " + s[j + 1:-1])).strip()
                    changed = True
                    break
    return s


def _clean_one(line):
    s = line
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    s = s.replace("\u3000", " ").replace("\xa0", " ").strip()
    if not s:
        return ""
    # 序号 / 标签 / 引号可能互相嵌套(「1. "标题"」),循环两遍足够
    for _ in range(2):
        s = _RE_LINE_NO.sub("", s, count=1).strip()
        s = _RE_LABEL.sub("", s, count=1).strip()
        s = _strip_quotes(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _has_content(s):
    """至少有一个中文/英文/数字,纯标点或纯 emoji 的行不算标题。"""
    return bool(re.search(r"[\w\u4e00-\u9fff]", s))


def split_titles(raw):
    """把粘贴文本拆成标题列表(已清理格式、已去重)。返回 (titles, stat)。"""
    if isinstance(raw, (list, tuple)):
        lines = [str(x) for x in raw]
    else:
        lines = re.split(r"\r\n|\r|\n", raw or "")

    titles = []
    seen = set()
    stat = {"raw": 0, "cleaned": 0, "empty": 0, "dup": 0}

    def push(text, original):
        if not text or not _has_content(text):
            stat["empty"] += 1
            return
        if text != original.strip():
            stat["cleaned"] += 1
        if text in seen:
            stat["dup"] += 1
            return
        seen.add(text)
        titles.append(text)

    for line in lines:
        if not line or not line.strip():
            continue
        stat["raw"] += 1
        # 整段粘成一行:以序号开头 + 中间还夹着序号 -> 先按行内序号切开
        if _RE_LINE_NO.match(line.strip()) and _RE_INLINE_NO.search(line):
            for part in _RE_INLINE_NO.split(line):
                if part and part.strip():
                    push(_clean_one(part), part)
            continue
        push(_clean_one(line), line)

    return titles, stat
