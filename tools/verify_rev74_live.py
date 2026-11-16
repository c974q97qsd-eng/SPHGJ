# -*- coding: utf-8 -*-
"""rev74 真机验证：多标签页串流不互相抢帧、关一个不全部断。

只读 + 两条临时 WS 连接；会短暂起两个独立 Chromium（用完即关），不改任何数据。
用法：python tools/verify_rev74_live.py <账号A> <账号B>（账号 ID 不入库）
"""
import json
import sys
import time
import urllib.request

import websocket

HOST = "192.168.1.27:8712"
# 账号一律由参数传入,脚本里不预置真实账号 ID。
# 挑两个当前没有远程标签页占用的账号:若某个账号已被用户打开,连接会被
# 「占用拒绝」挡住,验证结论会失真。
if len(sys.argv) < 3:
    print("用法: python tools/verify_rev74_live.py <账号A> <账号B>")
    sys.exit(2)
ACC_A, ACC_B = sys.argv[1], sys.argv[2]

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [%02d] PASS %s" % (PASS + FAIL, name))
    else:
        FAIL += 1
        print("  [%02d] FAIL %s  %s" % (PASS + FAIL, name, str(extra)[:200]))


def sessions():
    try:
        with urllib.request.urlopen("http://%s/api/remote/sessions" % HOST, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e), "items": []}


def ids():
    return sorted(x["account_id"] for x in sessions().get("items", []))


def connect(acc):
    url = "ws://%s/api/remote/ws?account=%s" % (HOST, acc)
    ws = websocket.create_connection(url, timeout=15)
    ws.settimeout(3)
    return ws


def wait_text(ws, want_t, timeout=90):
    """跳过二进制 JPEG 帧，等到指定 t 的 JSON 文本帧。"""
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        try:
            m = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            seen.append("EXC:" + type(e).__name__)
            break
        if isinstance(m, bytes):
            continue
        try:
            o = json.loads(m)
        except Exception:
            continue
        seen.append(o.get("t"))
        if o.get("t") == want_t:
            return o, seen
    return None, seen


def safe_close(ws):
    try:
        ws.close()
    except Exception:
        pass


def main():
    print("== 初始状态 ==")
    check("0 接口回报上限 4", sessions().get("max_sessions") == 4, sessions())
    # 用户自己可能正开着别的账号的远程标签页;只要本次两个目标账号空闲即可,
    # 绝不能为了测试去关掉用户正在用的那一路。
    check("1 本次两个目标账号无残留会话",
          ACC_A not in ids() and ACC_B not in ids(), ids())

    a = b = c = None
    try:
        print("\n== A. 同账号第二个标签页 ==")
        a = connect(ACC_A)
        hello, seen = wait_text(a, "hello", timeout=100)
        check("2 首个标签页建立串流(hello)", hello is not None, seen[:8])
        check("3 上限内会话已登记", ACC_A in ids(), ids())

        b = connect(ACC_A)
        err, seen_b = wait_text(b, "error", timeout=20)
        check("4 第二个标签页收到明确 error", err is not None, seen_b[:8])
        check("5 message 说清「另一个标签页」",
              err and "另一个标签页" in err.get("message", ""), err)
        check("6 拒绝后仍只有一路会话", ids() == [ACC_A], ids())
        safe_close(b)
        b = None

        print("\n== B. 不同账号两路并存 ==")
        c = connect(ACC_B)
        hello_c, seen_c = wait_text(c, "hello", timeout=100)
        check("7 第二个账号也建立串流", hello_c is not None, seen_c[:8])
        check("8 两路并存", ids() == sorted([ACC_A, ACC_B]), ids())

        print("\n== C. 关掉一路，另一路必须还在 ==")
        safe_close(a)
        a = None
        time.sleep(2.5)
        after = ids()
        check("9 关掉 A 后 B 仍在(核心修复)", after == [ACC_B], after)
        still = sessions().get("items", [])
        check("10 B 会话仍标记 owned",
              bool(still) and still[0].get("owned") is True, still)
        # B 还能继续收到画面帧吗
        got_frame = False
        end = time.time() + 8
        while time.time() < end:
            try:
                m = c.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if isinstance(m, bytes) and len(m) > 1000:
                got_frame = True
                break
        check("11 B 仍在持续推帧(没被一起关掉)", got_frame)

        safe_close(c)
        c = None
        time.sleep(2.5)
        check("12 全部关闭后无残留会话", ids() == [], ids())
    finally:
        for w in (a, b, c):
            if w is not None:
                safe_close(w)
        time.sleep(1.5)
        print("\n最终会话: %s" % ids())

    print("\n=== 结果: %d/%d 通过 ===" % (PASS, PASS + FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
