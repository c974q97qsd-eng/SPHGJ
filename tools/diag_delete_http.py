# -*- coding: utf-8 -*-
"""诊断：读取母本 run.log，抽取最后一次启动之后所有与「写/删除/API」相关的行。

run.log 是 GBK / UTF-8 混写，逐行双解码；UNC 读取需要脱离 sandbox 运行。
输出写到 D:\\SPHGJ\\_diag_del.txt，避免控制台编码问题。
"""
import io
import os

SRC = r"\\Sphgj\sphgj\run.log"
OUT = r"D:\SPHGJ\_diag_del.txt"


def dec(raw: bytes) -> str:
    best = None
    for enc in ("utf-8", "gbk"):
        try:
            s = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        bad = s.count("\ufffd")
        if best is None or bad < best[0]:
            best = (bad, s)
        if bad == 0:
            break
    if best is None:
        return raw.decode("gbk", "replace")
    return best[1]


def main():
    raw = open(SRC, "rb").read()
    lines = [dec(x).rstrip("\r") for x in raw.split(b"\n")]

    boot_idx = 0
    for i, l in enumerate(lines):
        if l.startswith("=====") and "启动" in l:
            boot_idx = i
    boot = lines[boot_idx:]

    keys = ("[api]", "删除", "del_comment", "写操作", "回复", "create_comment",
            "ERROR", "evaluate", "HTTP 直连", "hold_for_manual", "Target page",
            "退避", "风控", "block", "lock", "comment/")

    out = []
    out.append("file_bytes=%d total_lines=%d boot_marker=%r boot_lines=%d"
               % (len(raw), len(lines), lines[boot_idx] if lines else "", len(boot)))
    out.append("")
    out.append("===== 本次启动后：API / 写 / 删除 / 异常 相关 =====")
    hit = [l for l in boot if any(k in l for k in keys)]
    out.extend(hit if hit else ["(无匹配行)"])
    out.append("")
    out.append("===== 文件末尾 45 行 =====")
    out.extend(lines[-45:])
    out.append("")
    out.append("===== 全文最后 25 条含 del_comment / 写操作 的行 =====")
    hist = [l for l in lines if ("del_comment" in l or "写操作" in l)]
    out.extend(hist[-25:] if hist else ["(无匹配行)"])

    io.open(OUT, "w", encoding="utf-8").write("\n".join(out))
    print("wrote %s, boot_hits=%d, hist=%d" % (OUT, len(hit), len(hist)))


if __name__ == "__main__":
    main()
