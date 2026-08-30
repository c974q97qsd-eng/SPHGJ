"""SPHGJ 内存快照工具 —— 供定时采样调用,结果追加到 .workbuddy/mem_watch.log。

设计要点:
  - 完全自包含:程序没运行/API 未就绪时只记一行状态,不抛异常退出。
  - 自适应:dict 数超过 HOLDERS_THRESHOLD 才追加 ?holders=1(大容器定位 +
    引用链上溯)。该操作是 O(全部 gc 对象) 的重活,常态下没必要每次都跑。
  - 端口自适应:后端端口被占用会顺延,故探测 8712~8716。

用法:
    python tools/mem_snapshot.py            # 采样一次并追加到日志
    python tools/mem_snapshot.py --stdout   # 只打印,不写日志
"""
import json
import os
import sys
import time
import urllib.request

import psutil

PORTS = (8712, 8713, 8714, 8715, 8716)
HOLDERS_THRESHOLD = 500_000   # dict 超过此数才抓持有者
LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".workbuddy", "mem_watch.log")
PY = sys.executable


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def find_main():
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if (p.info["name"] or "").lower() == "python.exe":
            cl = p.info["cmdline"] or []
            if any("main.py" in str(c) for c in cl):
                try:
                    return psutil.Process(p.info["pid"])
                except Exception:
                    return None
    return None


def find_port():
    for port in PORTS:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=3)
            return port
        except Exception:
            continue
    return None


def api_get(port, path, timeout=300):
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout)
    return json.loads(r.read().decode("utf-8"))


def mb(x):
    return f"{x / 1024 / 1024:8.1f}MB"


def sample():
    lines = []
    main_p = find_main()
    if not main_p:
        lines.append(f"[{_ts()}] 程序未运行(未找到 main.py 进程)")
        return lines

    port = find_port()
    uptime_h = (time.time() - main_p.create_time()) / 3600
    lines.append(f"[{_ts()}] pid={main_p.pid}  已运行 {uptime_h:.1f}h  API端口={port}")

    try:
        subs = main_p.children(recursive=True)
    except Exception:
        subs = []
    groups = {}
    for c in subs:
        try:
            groups.setdefault(c.name(), []).append(c.memory_info().rss)
        except Exception:
            pass

    total = main_p.memory_info().rss
    lines.append(f"  主进程 python.exe{'':<20}{mb(total)}")
    for name, lst in sorted(groups.items(), key=lambda kv: -sum(kv[1])):
        s = sum(lst)
        total += s
        lines.append(f"  {name:<28} x{len(lst):<3}{mb(s)}")
    lines.append(f"  {'进程树总计':<32}{mb(total)}")

    if not port:
        lines.append("  API 未就绪,跳过对象统计")
        return lines

    try:
        d = api_get(port, "/api/system/memdiag")
        lines.append(f"  RSS={d['rss_mb']}MB  tracked={d['tracked_mb']}MB  untracked={d['untracked_mb']}MB")
        for t in d.get("top_types", [])[:3]:
            lines.append(f"    {t['type']:<12} {t['count']:>12,} 个   {t['size_mb']:>7}MB")

        n_dict = next((t["count"] for t in d.get("top_types", []) if t["type"] == "dict"), 0)
        if n_dict >= HOLDERS_THRESHOLD:
            lines.append(f"  !! dict={n_dict:,} 超过阈值 {HOLDERS_THRESHOLD:,},抓持有者...")
            h = api_get(port, "/api/system/memdiag?holders=1")
            if h.get("big_holders"):
                for x in h["big_holders"][:8]:
                    lines.append(f"    容器 {x['type']} len={x['len']:,}  {x['peek'][:60]}")
            else:
                lines.append("    (无 len>=20000 的容器)")
            if h.get("referrer_chain"):
                lines.append("    referrer_chain: " + json.dumps(h["referrer_chain"], ensure_ascii=False)[:800])
            if h.get("dict_key_samples"):
                for s in h["dict_key_samples"][:5]:
                    lines.append("    keys: " + str(s)[:150])
    except Exception as e:
        lines.append(f"  memdiag 调用失败: {type(e).__name__}: {e}")
    return lines


def main():
    out = sample()
    text = "\n".join(out)
    print(text)
    if "--stdout" not in sys.argv:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 62 + "\n")
        print(f"\n>> 已追加到 {LOG_PATH}")


if __name__ == "__main__":
    main()
