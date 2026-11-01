# -*- coding: utf-8 -*-
"""rev61 单测：① failed() 判定；② 补登时间窗不串号；③ 时间窗解析。

不依赖 playwright / fastapi，用 stub 顶掉 backend.wx_http。
"""
import os
import sys
import types
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, r"D:\SPHGJ")

# --- stub 掉 api_client 里的 httpx 依赖 ---
_stub = types.ModuleType("backend.wx_http")
_stub.post_json = None
_stub.backend_name = lambda: "stub"
sys.modules["backend.wx_http"] = _stub

from backend.storage import Storage            # noqa: E402
from backend.api_client import WxApiClient      # noqa: E402

fails = []


def ck(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


print("=== 1. failed() 判定 ===")
F = WxApiClient.failed
ck("300800 判失败(核心)", F({"errCode": 300800, "errMsg": "request failed"}) is True)
ck("errCode=0 判成功", F({"errCode": 0, "data": {"comment": {"commentId": "1"}}}) is False)
ck("空 dict 判成功", F({}) is False)
ck("__err 判失败", F({"__err": "no_page"}) is True)
ck("daily_write_limit 判失败", F({"__err": "daily_write_limit", "limit": 100}) is True)
ck("None 判失败", F(None) is True)
ck("ret=-1 判失败", F({"ret": -1}) is True)
ck("base_resp.ret!=0 判失败", F({"base_resp": {"ret": 300800}}) is True)
ck("errCode 字符串 '0' 判成功", F({"errCode": "0"}) is False)

print("=== 2. 补登时间窗 ===")
tmp = os.path.join(tempfile.gettempdir(), "rev61_test.db")
if os.path.exists(tmp):
    os.remove(tmp)
st = Storage(tmp)

FID = "fid_test"
EXP = "export/AAA"
CONTENT = "点头像，进入直播间领取优惠券下单！"

t_pending = datetime(2026, 10, 14, 1, 48, 29)   # 待确认登记时刻
ts_pending = int(t_pending.timestamp())

# 陷阱1：三个月前的同话术老评论（这正是线上被误认的那条）
st.upsert_comment(FID, EXP, {
    "commentId": "old_2026_0720", "commentNickname": "自己",
    "commentContent": CONTENT, "commentCreatetime": int(
        (t_pending - timedelta(days=86)).timestamp()), "likeCount": 0})
# 陷阱2：登记前 3 天的同话术评论
st.upsert_comment(FID, EXP, {
    "commentId": "old_2026_1011", "commentNickname": "自己",
    "commentContent": CONTENT, "commentCreatetime": int(
        (t_pending - timedelta(days=3)).timestamp()), "likeCount": 0})
# 正解：登记后 40s 真正补上的那条
st.upsert_comment(FID, EXP, {
    "commentId": "right_one", "commentNickname": "自己",
    "commentContent": CONTENT, "commentCreatetime": ts_pending + 40, "likeCount": 0})

st.mark_own_comment_pending(FID, EXP, CONTENT, "manual_reply", "acc1")
# 把登记时刻改回 10-14（线上真实场景：pending 是十几天前记的）
_c = st._conn()
_c.execute("UPDATE own_comment_pending SET created_at=? WHERE finder_id=?",
           (t_pending.isoformat(), FID))
_c.commit()
_c.close()
n = st.reconcile_own_comments()
ck("补登 1 条", n == 1)
own = [r[0] for r in st._conn().execute(
    "SELECT comment_id FROM own_comments WHERE finder_id=?", (FID,)).fetchall()]
ck("认到的是 right_one(不是 3 个月前的老评论)", own == ["right_one"])
ck("待确认已清空", st.pending_own_comment_count(FID) == 0)

print("=== 3. 时间窗边界 ===")
lo, hi = st._pending_time_window(t_pending.isoformat())
ck("下界 = 登记 - 600s", lo == ts_pending - 600)
ck("上界 = 登记 + 1800s", hi == ts_pending + 1800)
lo2, hi2 = st._pending_time_window("坏数据")
ck("解析失败退化为不限时间", (lo2, hi2) == (0, 2 ** 31))

try:
    os.remove(tmp)
except Exception as e:
    print("  (临时文件留在 %s: %s)" % (tmp, e))
print("RESULT", "ALL PASS" if not fails else "FAIL: %s" % fails)
sys.exit(1 if fails else 0)
