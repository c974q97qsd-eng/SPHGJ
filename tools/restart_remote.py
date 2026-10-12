# -*- coding: utf-8 -*-
"""远程重启 SPHGJ —— 在任意能访问到运行机的机器上执行。

用法:
    python tools/restart_remote.py <host>          # 指定运行机主机名/IP, 重启
    python tools/restart_remote.py <host> 5        # 指定 delay 秒
    python tools/restart_remote.py <host> --check  # 只看运行状态, 不重启
    python tools/restart_remote.py                 # 省略 host 时取环境变量 SPHGJ_HOST

    set SPHGJ_HOST=<运行机主机名>                  # Windows 先设一次, 之后可省略 host

原理:调 POST /api/system/restart 让运行机「优雅退出 + 按原方式重新拉起」,
然后轮询 GET /api/system/info —— **进程号变了且服务可访问 = 重启成功**。

前提:运行机上跑的程序必须是带重启通道的版本(backend/restart.py)。
      旧版本调这个接口会 404,那就得在运行机本机双击「重启-SPHGJ.vbs」升级一次。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 运行机地址不写死在仓库里:优先命令行参数,其次环境变量,默认本机
DEFAULT_HOST = os.environ.get("SPHGJ_HOST", "127.0.0.1")
PORTS = (8712, 8713, 8714, 8715)


def api(host, port, path, method="GET", timeout=4):
    url = "http://%s:%d%s" % (host, port, path)
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(host):
    """找到在跑的实例,返回 (port, info)。找不到返回 (0, None)。"""
    for p in PORTS:
        try:
            d = api(host, p, "/api/system/info", timeout=3)
            if d.get("pid"):
                return p, d
        except Exception:
            continue
    return 0, None


def probe_any(host):
    """不依赖新接口:只用 /api/config 判断有没有实例(用于旧版本)。"""
    for p in PORTS:
        try:
            api(host, p, "/api/config", timeout=3)
            return p
        except Exception:
            continue
    return 0


def show(port, info):
    print("  端口      : %s" % port)
    print("  进程号    : %s" % info.get("pid"))
    print("  版本      : %s" % info.get("version"))
    print("  启动时间  : %s" % info.get("started_at"))
    print("  模式      : %s" % ("headless(无窗口)" if info.get("headless") else "桌面窗口"))
    print("  程序目录  : %s" % info.get("root"))
    if info.get("restart"):
        print("  上次重启  : %s" % json.dumps(info["restart"], ensure_ascii=False))


def main():
    args = [a for a in sys.argv[1:]]
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    host = args[0] if args else DEFAULT_HOST
    delay = float(args[1]) if len(args) > 1 else 3.0

    print("=" * 68)
    print("目标主机: %s" % host)
    print("=" * 68)

    port, info = probe(host)
    if not port:
        legacy = probe_any(host)
        if legacy:
            print("!! %s:%s 有实例在跑,但 /api/system/info 返回 404" % (host, legacy))
            print("   说明它还是【没有重启通道的旧版本】,无法远程重启。")
            print("   请在运行机本机双击项目根目录的「重启-SPHGJ.vbs」升级一次,")
            print("   之后就能一直远程重启了。")
            return 2
        print("!! %s 上没找到在跑的实例(试过端口 %s)" % (host, list(PORTS)))
        return 2

    print("当前运行状态:")
    show(port, info)
    old_pid = int(info["pid"])

    if check_only:
        print("\n(--check 模式,不做任何操作)")
        return 0

    print("\n>>> 触发重启(%.0f 秒后退出)…" % delay)
    try:
        res = api(host, port, "/api/system/restart?delay=%g" % delay, method="POST", timeout=10)
    except urllib.error.HTTPError as e:
        print("!! 重启请求失败 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 1
    except Exception as e:
        print("!! 重启请求失败: %r" % (e,))
        return 1
    print("    已受理: %s" % json.dumps(res, ensure_ascii=False))
    print("    旧进程号 %s,重启助手 %s" % (res.get("old_pid"), res.get("helper_pid")))

    t0 = time.time()
    new_pid = new_port = 0
    deadline = t0 + 240
    last_note = 0
    while time.time() < deadline:
        elapsed = time.time() - t0
        p, d = probe(host)
        if p and d and int(d.get("pid", 0)) != old_pid:
            new_pid, new_port = int(d["pid"]), p
            break
        if elapsed - last_note >= 15:
            last_note = elapsed
            print("    …等待中(%.0fs)旧进程仍在=%s" % (elapsed, bool(p and d and int(d.get("pid", 0)) == old_pid)))
        time.sleep(1)

    print()
    if not new_pid:
        print("!! 等待 240 秒仍未见新进程。排查建议:")
        print("   1) 运行机上是否弹了错误框 / 起不来(看 run.log 尾部)")
        print("   2) 运行机本机看 data/restart_helper.log 与 data/restart_status.json")
        print("   3) 兜底:在运行机本机双击「重启-SPHGJ.vbs」")
        return 1

    print(">>> 重启成功(耗时 %.1fs)" % (time.time() - t0))
    print("    旧进程号 %s -> 新进程号 %s,端口 %s" % (old_pid, new_pid, new_port))
    try:
        d = api(host, new_port, "/api/system/info", timeout=5)
        print("\n新实例状态:")
        show(new_port, d)
    except Exception as e:
        print("   (读新实例信息失败: %r)" % (e,))

    # 顺带验一下评论分页接口真的可用
    try:
        c = api(host, new_port, "/api/comments?limit=3&offset=0", timeout=15)
        print("\n评论接口自检: 共 %s 条, 本次返回 %s 条(limit=3)"
              % (c.get("total"), len(c.get("items", []))))
        c2 = api(host, new_port, "/api/comments?limit=3&offset=3", timeout=15)
        same = [x["comment_id"] for x in c.get("items", [])] == \
               [x["comment_id"] for x in c2.get("items", [])]
        print("   offset=3 返回不同内容: %s" % (not same))
    except Exception as e:
        print("\n(评论接口自检失败: %r)" % (e,))

    return 0


if __name__ == "__main__":
    sys.exit(main())
