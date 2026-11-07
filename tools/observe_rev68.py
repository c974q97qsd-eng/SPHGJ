# -*- coding: utf-8 -*-
"""只读观察：重启后 rev68 是否生效（受限识别 / 重建节流）。"""
import collections
import io
import re
import sys

RAW = r"\\Sphgj\sphgj\run.log"


def dec(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


rows = [dec(x) for x in io.open(RAW, "rb").read().split(b"\n")]
rows = [l for l in rows if l.strip()]
print("run.log 行数 =", len(rows))
print("首行:", rows[0][:100])
print("末行:", rows[-1][:100])

# 只取最后一次「===== 启动 =====」之后的行 —— run.log 从 9-13 起累积，
# 按 HH:MM 前缀过滤会把历史同小时的行全部算进来（踩过）。
start = 0
for i, l in enumerate(rows):
    if "启动 =====" in l or "服务启动" in l:
        start = i
tail = rows[start:]
print("\n最后一次启动标记: %s" % rows[start][:90] if start else "\n没找到启动标记")
print("之后共 %d 行，首行 %s" % (len(tail), tail[0][:80]))

pat = collections.Counter()
rules = [
    ("重启后启动", r"服务启动|恢复\]|正在恢复"),
    ("定期 close+重建", r"live_page 定期 close\+重建"),
    ("大屏受限 WARNING(rev68)", r"大屏页提示「暂时无法使用该功能」"),
    ("打开大屏页取流", r"打开直播大屏页取流"),
    ("非直播,关闭 live_page", r"非直播,关闭 live_page"),
    ("信号丢失重载", r"信号丢失重载"),
    ("直播信号中断", r"直播信号中断"),
    ("开始流式推送", r"开始流式推送"),
    ("已注入登录态", r"已注入登录态"),
    ("文本采样", r"直播页文本采样"),
]
for l in tail:
    for name, rx in rules:
        if re.search(rx, l):
            pat[name] += 1
for k, v in pat.most_common():
    print(" %-26s %d" % (k, v))

print("\n重建 / 受限 / 开页 相关原文:")
n = 0
for l in tail:
    if re.search(r"定期 close\+重建|暂时无法使用该功能|打开直播大屏页取流|dashboard 数据不可用", l):
        print("  ", l[:175])
        n += 1
        if n >= 25:
            break
