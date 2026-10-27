# -*- coding: utf-8 -*-
"""按行智能解码母本 run.log（GBK / UTF-8 混写），输出干净文本。

MEMORY 里记的是「run.log 是 GBK」，但实测 03:04~03:06 那段是 UTF-8。
逐行试两种解码，取替换字符更少的那个，才不会把「直播检测」读成「鐩存挱妫�娴嬪洖」。
"""
import io
import sys


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
    src = r"\\Sphgj\sphgj\run.log"
    raw = open(src, "rb").read()
    lines = [dec(x) for x in raw.split(b"\n")]
    lines = [x.rstrip("\r") for x in lines]

    mode = sys.argv[1] if len(sys.argv) > 1 else "tail"
    if mode == "grep":
        kw = sys.argv[2]
        sel = [l for l in lines if kw in l]
    elif mode == "win":
        a, b = sys.argv[2], sys.argv[3]
        sel = [l for l in lines if a <= l[:8] <= b]
    else:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 80
        sel = lines[-n:]

    out = "\n".join(sel)
    io.open(r"D:\SPHGJ\_win.txt", "w", encoding="utf-8").write(out)
    print("lines=%d -> _win.txt" % len(sel))


if __name__ == "__main__":
    main()
