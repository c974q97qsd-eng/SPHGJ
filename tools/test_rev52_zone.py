# -*- coding: utf-8 -*-
"""rev52 单元测试:分区 / 分层模式 / 重试判定 / 播放数基线。"""
import sys
import types
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

# 本地没有 playwright(它跑在母本上),导入 account_manager 需要 stub
if "playwright" not in sys.modules:
    pw = types.ModuleType("playwright")
    apw = types.ModuleType("playwright.async_api")
    for name in ("async_playwright", "Error", "TimeoutError", "Page", "BrowserContext"):
        setattr(apw, name, type(name, (Exception,), {}))
    pw.async_api = apw
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = apw

sys.path.insert(0, r"D:\SPHGJ")

from backend import video_zone                     # noqa: E402
from backend.comment_fetcher import CommentFetcher  # noqa: E402
from backend.storage import Storage                # noqa: E402

PASS = FAIL = 0


def chk(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name)


def vids(n, base_read=0):
    return [{"objectId": "v%03d" % i, "readCount": base_read + i, "createTime": 1000 - i}
            for i in range(n)]


print("=" * 70)
print("[1] video_zone.classify —— 主口径 growth")
v = vids(100)
g = {"v%03d" % i: (i * 10 if i % 5 == 0 else 0) for i in range(100)}
r = video_zone.classify(v, g)
chk(r["basis"] == "growth", "依据=growth")
chk(len(r["ordered"]) == 100, "全部进入排序")
chk(r["counts"]["heat"] == 30 and r["counts"]["normal"] == 50 and r["counts"]["cold"] == 20,
    "分区 30/50/20")
chk(r["zones"]["v095"] == "heat", "增长最高(v095 增 950)进热区")
# 增长为 0 的 80 个之间按播放数降序 —— 播放最低的落到冷区
chk(r["zones"]["v004"] == "cold", "增长为 0 且播放最低的落冷区")
chk(r["zones"]["v099"] == "heat", "增长为 0 但播放最高 -> 仍进热区(降级排序)")

print()
print("[2] 降级 —— growth 全缺失时用 readCount")
r2 = video_zone.classify(v, {})
chk(r2["basis"] == "read", "依据=read")
chk(r2["zones"]["v099"] == "heat", "播放最高(v099)进热区")
chk(r2["zones"]["v000"] == "cold", "播放最低进冷区")

print()
print("[3] 降级 —— 连 readCount 都没有时保持原顺序")
v_noread = [{"objectId": "n%03d" % i} for i in range(50)]
r3 = video_zone.classify(v_noread, {})
chk(r3["basis"] == "order", "依据=order")
# 50 个作品:rank 1-30 heat、31-50 normal(还没到 80 的冷区门槛)
chk(r3["zones"]["n000"] == "heat", "前 30 按顺序进热区")
chk(r3["zones"]["n049"] == "normal", "第 50 名仍在普通区(未超 80)")

print()
print("[4] 部分视频有增长、部分没有(新视频),不能把未知的压进冷区")
v4 = vids(60)
g4 = {"v%03d" % i: 0 for i in range(0, 60, 2)}   # 偶数有基线且增长 0
r4 = video_zone.classify(v4, g4)
chk(r4["basis"] == "growth", "依据=growth")
chk("v001" in r4["zones"], "无基线的也参与分区")
chk(r4["counts"]["heat"] == 30, "热区仍为 30")

print()
print("[5] wanted —— 三种模式的过滤")
z = r["zones"]
w_heat = video_zone.wanted(z, full=False, include_normal=False)
w_norm = video_zone.wanted(z, full=False, include_normal=True)
w_full = video_zone.wanted(z, full=True, include_normal=True)
chk(len(w_heat) == 30, "热区轮 30 个")
chk(len(w_norm) == 80, "普通区轮 80 个")
chk(len(w_full) == 100, "全量轮 100 个")

print()
print("[6] CommentFetcher._mode —— 轮次决定模式(冷扫每 24、普通区每 3)")
f = CommentFetcher(None, None, "test", config={"scan_cold_every": 24, "scan_normal_every": 3})
modes = []
for i in range(1, 25):
    f._round = i
    modes.append(f._mode())
chk(modes.count("full") == 1 and modes[23] == "full", "第 24 轮是 full")
chk(modes.count("normal") == 7, "普通区轮 7 次(3/6/9/12/15/18/21)")
chk(modes.count("heat") == 16, "其余 16 轮只扫热区")

print()
print("[7] CommentFetcher._err_code —— 各种响应形态")
chk(CommentFetcher._err_code({"errCode": 0}) == 0, "errCode 0")
chk(CommentFetcher._err_code({"errCode": -1}) == -1, "errCode -1")
chk(CommentFetcher._err_code({"errCode": "300333"}) == 300333, "字符串错误码")
chk(CommentFetcher._err_code({"ret": 0}) == 0, "只有 ret")
chk(CommentFetcher._err_code(None) == -1, "None 视为失败")
chk(300333 in CommentFetcher.NO_RETRY_CODES, "300333 不重试")

print()
print("[8] 重试:第一次 -1、第二次成功,应返回成功")
calls = {"n": 0}


async def flaky(**kw):
    calls["n"] += 1
    if calls["n"] == 1:
        return {"errCode": -1, "errMsg": "busy"}
    return {"errCode": 0, "data": {"list": [{"objectId": "a"}]}}


async def _t8():
    f2 = CommentFetcher(None, None, "t", config={})
    f2.RETRY_DELAYS = (0, 0.01, 0.02)
    r = await f2._call(flaky)
    return r


r8 = asyncio.run(_t8())
chk(r8.get("errCode") == 0, "重试后成功")
chk(calls["n"] == 2, "共请求 2 次")

print()
print("[9] 重试:300333 登录失效不重试")
calls2 = {"n": 0}


async def expired(**kw):
    calls2["n"] += 1
    return {"errCode": 300333}


async def _t9():
    f3 = CommentFetcher(None, None, "t", config={})
    f3.RETRY_DELAYS = (0, 0.01, 0.02)
    return await f3._call(expired)


r9 = asyncio.run(_t9())
chk(r9.get("errCode") == 300333, "返回 300333")
chk(calls2["n"] == 1, "只请求 1 次(不浪费时间)")

print()
print("[10] storage.video_activity —— 采样、3 日基线、增长计算")
tmp = os.path.join(tempfile.gettempdir(), "sphgj_rev52_test.db")
for p in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(p):
        os.remove(p)
st = Storage(tmp)
FID = "finder_test"
today = datetime.now().strftime("%Y-%m-%d")
d3 = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

n = st.upsert_video_activity(FID, [
    {"objectId": "a", "readCount": 500, "likeCount": 5, "commentCount": 2},
    {"objectId": "b", "readCount": 100},
    {"objectId": "c"},                       # 缺 readCount -> 跳过
], "acc")
chk(n == 2, "只写入有播放数的 2 条(缺字段跳过,不写脏 0)")

base_now = st.get_activity_baseline(FID, days=3)
chk(base_now == {}, "今天的数据不算基线(要 3 天前)")

c = st._conn()
c.execute("INSERT INTO video_activity(finder_id,export_id,day,read_count,updated_at) "
          "VALUES(?,?,?,?,?)", (FID, "a", d3, 300, "x"))
c.execute("INSERT INTO video_activity(finder_id,export_id,day,read_count,updated_at) "
          "VALUES(?,?,?,?,?)", (FID, "a", d1, 420, "x"))   # 更近但仍在 3 天窗口内
c.execute("INSERT INTO video_activity(finder_id,export_id,day,read_count,updated_at) "
          "VALUES(?,?,?,?,?)", (FID, "b", d3, 90, "x"))
c.commit()
c.close()

base = st.get_activity_baseline(FID, days=3)
# cutoff = today-3d,所以 d1(1 天前)落在窗口内被排除,基线必须是 d3=300
chk(base.get("a") == 300, "取 3 天前或更早(d3=300,1 天前的 420 不算基线)")
chk(base.get("b") == 90, "b 基线 90")
chk("c" not in base, "c 没有采样,不在基线里")

cur = {"a": 500, "b": 100}
grow = {k: cur[k] - base[k] for k in base if k in cur}
chk(grow["a"] == 200 and grow["b"] == 10, "增长 a=200(500-300) b=10")
r10 = video_zone.classify(
    [{"objectId": "a", "readCount": 500}, {"objectId": "b", "readCount": 100}],
    grow, heat_top=1, normal_top=2)
chk(r10["zones"]["a"] == "heat", "增长高的 a 进热区")

print()
print("[11] 同一天重复采样只覆盖,不新增行")
st.upsert_video_activity(FID, [{"objectId": "a", "readCount": 777}], "acc")
c = st._conn()
rows = c.execute("SELECT COUNT(*), MAX(read_count) FROM video_activity "
                 "WHERE finder_id=? AND export_id='a' AND day=?", (FID, today)).fetchone()
c.close()
chk(rows[0] == 1 and rows[1] == 777, "仍 1 行,值被更新为 777")

for p in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(p):
        os.remove(p)

print()
print("=" * 70)
print("结果: PASS %d / FAIL %d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
