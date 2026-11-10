# -*- coding: utf-8 -*-
"""rev70: 登录态事件档案 —— 掉线 / 上线 / 被顶 / 软故障的流水账。

## 为什么要单独落一份

run.log 会被轮转掉(rev69 起 20MB 或 6 小时就归档,只留少数几份)。而
「这个账号到底是**被别处登录顶掉**的、还是**自然过期**的、一天掉几次、
恢复得快不快」—— 这些结论必须能回溯好几天,靠 grep run.log 是在跟轮转赛跑。
(2026-11-10 那次溯源:23 次真失效 / 0 次自然过期,就是把 run.log 反解出来的,
过程很痛苦。以后这类问题直接读这里。)

一条事件一行,append-only,JSON 数组,超过 MAX_EVENTS 砍最老的。

## 为什么不做成数据库

事件量极小(8 个账号 × 每天几十条)。用 JSON 能让用户直接打开看,也能直接
`git diff` 看出行为变化 —— 没必要为这点数据引入 sqlite 的耦合。

## 与在网状态机的关系

本模块**只记录,不参与判定**。判定仍然只看 account_manager.mark_offline /
mark_online 里的确定性证据。这里挂了也不能影响主流程,所以全部 try/except 吞掉。
"""
import json
import os
import threading
import time

logger_name = "sphgj"
MAX_EVENTS = 3000
_VERSION = 1

_lock = threading.Lock()
_cache = None       # {"version":1,"events":[...]} 进程内缓存,避免每次都读盘


def _path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "auth_events.json")


def _load():
    """读档案(带缓存)。文件坏了就当空档案重来 —— 不因为日志坏了挡住主流程。"""
    global _cache
    if _cache is not None:
        return _cache
    p = _path()
    data = None
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        data = {"version": _VERSION, "events": []}
    data["version"] = _VERSION
    _cache = data
    return _cache


def _save(data):
    """原子写(临时文件 + replace)。失败只记不抛。"""
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def record(account_id, kind, code=None, reason="", source="", extra=None):
    """记一条事件。返回是否写盘成功。

    kind: offline(判失效) / online(恢复) / http_expired(300333) /
          http_kicked(300342 被顶) / http_badauth(其它错误码) /
          soft_backoff(退避) / soft_recover(退避后恢复) / scan_ok(扫码成功)
    """
    try:
        ev = {
            "ts": int(time.time()),
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "account_id": str(account_id or ""),
            "kind": str(kind or ""),
            "code": code,
            "reason": (reason or "")[:200],
            "source": (source or "")[:40],
        }
        if extra:
            ev["extra"] = extra
        with _lock:
            data = _load()
            events = data["events"]
            events.append(ev)
            if len(events) > MAX_EVENTS:
                del events[:len(events) - MAX_EVENTS]
            return _save(data)
    except Exception:
        return False


def recent(account_id=None, limit=200, kinds=None):
    """按时间倒序取最近事件。account_id=None 表示所有账号。"""
    try:
        with _lock:
            events = list(_load()["events"])
    except Exception:
        return []
    out = []
    for ev in reversed(events):
        if account_id and ev.get("account_id") != str(account_id):
            continue
        if kinds and ev.get("kind") not in kinds:
            continue
        out.append(ev)
        if len(out) >= max(1, int(limit or 200)):
            break
    return out


def summary(days=7):
    """按账号 + kind 汇总最近 days 天的次数。给前端/诊断脚本用。"""
    since = int(time.time()) - max(1, int(days or 7)) * 86400
    try:
        with _lock:
            events = list(_load()["events"])
    except Exception:
        return {}
    agg = {}
    for ev in events:
        if int(ev.get("ts") or 0) < since:
            continue
        aid = ev.get("account_id") or "?"
        kind = ev.get("kind") or "?"
        slot = agg.setdefault(aid, {})
        slot[kind] = slot.get(kind, 0) + 1
    return agg


def reset_cache_for_test():
    """单测用:丢掉进程内缓存,强制下次读盘。"""
    global _cache
    with _lock:
        _cache = None
