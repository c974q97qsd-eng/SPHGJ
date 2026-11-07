# -*- coding: utf-8 -*-
"""rev68 单测：大屏受限识别 + 退避约束（本机无 playwright，用源码 + 表达式验证）。"""
import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LF = io.open(os.path.join(ROOT, "backend", "live_fetcher.py"), encoding="utf-8").read()
AM = io.open(os.path.join(ROOT, "backend", "account_manager.py"), encoding="utf-8").read()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, extra))


print("=== A. 受限词表与判定 ===")
m = re.search(r"_DOM_BLOCKED_WORDS = (\(.*?\))\n", LF, re.S)
check("A1 词表已定义", bool(m), "没找到 _DOM_BLOCKED_WORDS")
WORDS = ast.literal_eval(m.group(1)) if m else ()
check("A2 含主文案「暂时无法使用该功能」", "暂时无法使用该功能" in WORDS, WORDS)
check("A3 词表是元组且非空", isinstance(WORDS, tuple) and len(WORDS) >= 2, WORDS)


def blocked(txt):
    """与 live_fetcher.read_dom_state 里的检测表达式同构。"""
    return bool(txt) and any(w in txt for w in WORDS)


check("A4 受限页文本 -> True", blocked("暂时无法使用该功能 我知道了"))
check("A5 正常直播页 -> False", not blocked("正在直播 直播中 在线人数 100"))
check("A6 未播文案 -> False", not blocked("未开播 暂无直播"))
check("A7 空文本 -> False", not blocked(""))
check("A8 None -> False", not blocked(None))

print("\n=== B. live_fetcher 侧落地 ===")
check("B1 dom_blocked 默认 False", "self.dom_blocked = False" in LF)
check("B2 采样里赋值 dom_blocked", "self.dom_blocked = bool(txt) and any(" in LF)
check("B3 赋值在 read_dom_state_from_text 之前（先认受限再认未播）",
      LF.index("self.dom_blocked = bool(txt)") < LF.index("state, matched = read_dom_state_from_text(txt)"))
check("B4 snapshot 带出 dom_blocked", '"dom_blocked": self.dom_blocked,' in LF)

print("\n=== C. account_manager 侧落地 ===")
check("C1 退避字段初始化", "self._dash_blocked_until = 0.0" in AM)
check("C2 新页拨重建计时器", "self._last_live_reload = time.time()" in AM)
check("C3 退避秒数可配、默认 900",
      'self.config.get("dashboard_blocked_backoff_sec", 900)' in AM)
check("C4 判定前检测 dom_blocked", 'getattr(lf, "dom_blocked", False)' in AM)
check("C5 受限时打 WARNING 说明 dashboard 不可用",
      "dashboard 数据不可用" in AM and "暂时无法使用该功能" in AM)

# 重建 / 重载条件必须带 now >= 退避截止
m_rebuild = re.search(
    r"if self\.live_page and now - self\._last_live_reload >= rebuild_sec(.*?):\n", AM, re.S)
check("C6 重建条件带退避约束",
      bool(m_rebuild) and "_dash_blocked_until" in (m_rebuild.group(1) or ""),
      m_rebuild.group(1) if m_rebuild else "没找到重建条件")
m_reload = re.search(r"if idle > 120 and can_reload(.*?):\n", AM, re.S)
check("C7 重载条件带退避约束",
      bool(m_reload) and "_dash_blocked_until" in (m_reload.group(1) or ""),
      m_reload.group(1) if m_reload else "没找到重载条件")

print("\n=== D. 推流不被误伤 ===")
# dom_blocked 分支里不能有 return / 关页动作：受限只记退避，不阻断 is_live 判定
blk = re.search(r'if getattr\(lf, "dom_blocked", False\):(.*?)\n            # 判定:',
                AM, re.S)
body = blk.group(1) if blk else ""
check("D1 dom_blocked 分支存在", bool(body), "没抓到分支体")
check("D2 分支内没有 return", "return" not in body, body[:120])
check("D3 分支内不关页", "live_page.close" not in body and "lf.close" not in body)
# judge_live 调用不得引入 dom_blocked（受限不等于未播）
judge_calls = re.findall(r"judge_live\((.*?)\)\n", AM, re.S)
check("D4 judge_live 调用里没有 dom_blocked",
      all("dom_blocked" not in c for c in judge_calls), len(judge_calls))

print("\n=== E. 退避逻辑求值 ===")


def cond(now, last, rebuild_sec, blocked_until):
    return bool(last) and (now - last) >= rebuild_sec and now >= blocked_until


check("E1 未到周期不重建", not cond(1000, 900, 1800, 0))
check("E2 到周期且未退避 -> 重建", cond(2800, 900, 1800, 0))
check("E3 到周期但退避中 -> 不重建", not cond(2800, 900, 1800, 3000))
check("E4 退避过期 -> 恢复重建", cond(3600, 900, 1800, 3000))

print("\n=== F. 现有语义未被破坏 ===")
check("F1 judge_live 仍接收 status 参数", "status=info.get(\"status_num\")" in AM)
check("F2 非直播关页逻辑仍在", "非直播,关闭 live_page 省内存" in AM)
check("F3 信号丢失重试仍在", "直播信号中断" in AM)

print("\n结果：PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
