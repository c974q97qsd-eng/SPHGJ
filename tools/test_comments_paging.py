# -*- coding: utf-8 -*-
"""评论列表分页 回归测试(不启动浏览器:SQL 分页用 Storage 真库跑,前端做源码/产物静态检查)。

背景(为什么要有这些用例):
  评论页曾经一次渲染 300 行 —— limit 写死 300、没有 offset,想看老评论只能改代码。
  每行 8 个单元格 ≈ 2400 个 DOM 节点,WebView2 里滚动和勾选都明显卡顿。

现在的约定(改动前先读这条,别改回去):
  a) 后端 /api/comments 走标准 SQL 分页:LIMIT ? OFFSET ?,total 用 COUNT(*) 单独算,
     total 不随 limit/offset 变化;
  b) 前端每页 50/100/200(默认 50),offset = (页码-1)*limit,筛选变化一律回第 1 页;
  c) WS 新评论只在第 1 页前置插入(否则会打乱当前页的窗口),其它页只更新 total;
  d) 末页被删空自动回退,不停在空白页。

数据安排(每个账号职责单一,免得期望值互相干扰):
  acc1  123 条纯客户评论、全未回复     -> A 组分页语义
  acc2  123 条纯客户评论(内容不同)    -> B 组 account_id 过滤
  acc3  10 条已回复 + 3 条本号自己发的 + 5 条已删除 -> B 组筛选与 hide_own

跑法: 配置环境安装\\Python314\\python.exe tools/test_comments_paging.py
"""
import io
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.storage import Storage          # noqa: E402

PASS = 0
FAIL = 0

N = 123                     # 故意不是任何 limit 的整数倍
SELF_CONTENT = "点头像，进入直播间领取优惠券下单！"


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def insert_comment(c, account_id, i, content="", nickname="", author_id="",
                   replied=0, deleted=0):
    """插入一条评论;create_time 递增,排序稳定可预期。"""
    content = content or ("评论内容 %05d 你好" % i)
    c.execute(
        """INSERT INTO comments(account_id,export_id,comment_id,author_id,nickname,
             content,head_url,create_time,like_count,read_flag,replied,deleted,raw,fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, "exp_%s_%05d" % (account_id, i),
         "cid_%s_%05d" % (account_id, i), author_id,
         nickname or ("客户%05d" % i), content, "", 1750000000 + i,
         i % 7, 0, replied, deleted, "{}", "2026-10-12T20:00:00"))


# ===================================================================== 准备
tmp = tempfile.mkdtemp(prefix="sphgj_cmpg_")
st = Storage(os.path.join(tmp, "t.db"))
conn = st._conn()
for i in range(N):
    insert_comment(conn, "acc1", i)
    insert_comment(conn, "acc2", i, content="ACC2 的评论 %05d" % i)
for i in range(10):                                  # acc3:已回复
    insert_comment(conn, "acc3", i, content="已回复测试 %02d" % i, replied=1)
for i in range(3):                                   # acc3:本号自己发的
    insert_comment(conn, "acc3", 100 + i, content=SELF_CONTENT,
                   nickname="U.S.POLOASSN.鞋类店", author_id="v2_self")
for i in range(5):                                   # acc3:已删除
    insert_comment(conn, "acc3", 200 + i, deleted=1)
conn.commit()
conn.close()

print("=" * 72)
print("A. 后端 SQL 分页语义 (acc1 共 %d 条)" % N)
print("=" * 72)

i50_0, t50 = st.query_comments(account_id="acc1", limit=50, offset=0)
i50_50, _ = st.query_comments(account_id="acc1", limit=50, offset=50)
i50_100, t50b = st.query_comments(account_id="acc1", limit=50, offset=100)
i200_0, t200 = st.query_comments(account_id="acc1", limit=200, offset=0)
i_extra, t_extra = st.query_comments(account_id="acc1", limit=50, offset=123)

chk("total 与 limit 无关(50 与 200 都给 123)", t50 == t200 == N,
    "t50=%s t200=%s" % (t50, t200))
chk("total 与 offset 无关(越界页仍报 123)", t_extra == N, "t=%s" % t_extra)
chk("首页条数 = limit", len(i50_0) == 50, "len=%d" % len(i50_0))
chk("第二页条数 = limit", len(i50_50) == 50, "len=%d" % len(i50_50))
chk("末页条数 = 余数(123-100=23)", len(i50_100) == 23, "len=%d" % len(i50_100))
chk("越界 offset 返回空列表", i_extra == [], "len=%d" % len(i_extra))

ids = [x["comment_id"] for x in i50_0 + i50_50 + i50_100]
chk("三页拼接 = 全集(123 条,无重复)", len(ids) == len(set(ids)) == N,
    "len=%d uniq=%d" % (len(ids), len(set(ids))))
chk("三页拼接与 200 条首页的集合一致(无遗漏)",
    set(ids) == set(x["comment_id"] for x in i200_0))

ct = [x["create_time"] for x in i200_0]
chk("按 create_time 倒序", ct == sorted(ct, reverse=True), "head=%s" % ct[:3])
chk("分页边界不重复:第 2 页首条 != 第 1 页末条",
    i50_50[0]["comment_id"] != i50_0[-1]["comment_id"])
chk("第 2 页首条的时间严格早于第 1 页末条",
    i50_50[0]["create_time"] < i50_0[-1]["create_time"])
chk("返回字段与前端 Comment 类型一致",
    set(i50_0[0].keys()) == {"account_id", "export_id", "comment_id", "nickname",
                             "content", "head_url", "create_time", "like_count",
                             "replied", "fetched_at"},
    str(sorted(i50_0[0].keys())))

print()
print("=" * 72)
print("B. 筛选 × 分页")
print("=" * 72)

_, t_acc2 = st.query_comments(account_id="acc2", limit=50, offset=0)
chk("account_id 过滤后 total=123", t_acc2 == N, "t=%s" % t_acc2)

_, t_all = st.query_comments(limit=50, offset=0)
chk("不传 account_id:123 + 123 + acc3 未删 13 = 259", t_all == 259, "t=%s" % t_all)

qi, qt = st.query_comments(account_id="acc1", q="你好", limit=50, offset=0)
chk("q 搜索命中内容(total=123,首页 50 条)", qt == N and len(qi) == 50,
    "t=%s len=%d" % (qt, len(qi)))

_, qn = st.query_comments(account_id="acc1", q="客户00001", limit=50, offset=0)
chk("q 也能匹配昵称", qn == 1, "t=%s" % qn)

_, r_true = st.query_comments(account_id="acc3", replied=True, limit=50, offset=0)
chk("replied=True 过滤后 total=10", r_true == 10, "t=%s" % r_true)

_, r_false = st.query_comments(account_id="acc3", replied=False, limit=50, offset=0)
chk("replied=False 过滤后 total=3(只剩本号自己发的 3 条)", r_false == 3,
    "t=%s" % r_false)

OWN_MAP = {"acc3": {"ids": ["v2_self"], "names": ["U.S.POLOASSN.鞋类店"]}}
hi, ht = st.query_comments(account_id="acc3", limit=50, offset=0, hide_own=True,
                           own_map=OWN_MAP)
chk("hide_own 后 total=10(排掉自己发的 3 条)", ht == 10, "t=%s" % ht)
chk("hide_own 后列表里没有自己那条的内容",
    all(x["content"] != SELF_CONTENT for x in hi), str([x["content"] for x in hi][:3]))
chk("hide_own 后剩下的都是已回复的 10 条",
    len(hi) == 10 and all(x["replied"] for x in hi))

_, hq = st.query_comments(account_id="acc3", q="直播间", limit=50, offset=0,
                          hide_own=True, own_map=OWN_MAP)
chk("组合筛选(q=直播间 + hide_own)后 total=0(自己发的被搜到又被隐藏)", hq == 0,
    "t=%s" % hq)

_, hq2 = st.query_comments(account_id="acc3", q="直播间", limit=50, offset=0)
chk("不 hide_own 时 q=直播间 能搜到自己那 3 条", hq2 == 3, "t=%s" % hq2)

_, comb = st.query_comments(account_id="acc3", q="已回复", replied=True, limit=50, offset=0)
chk("组合筛选(q + replied=True)后 total=10", comb == 10, "t=%s" % comb)

chk("已删除的评论不出现在任何结果里",
    all("cid_acc3_002" not in x["comment_id"] for x in
        st.query_comments(account_id="acc3", limit=50, offset=0)[0]))

# ===================================================================== C 前端源码
print()
print("=" * 72)
print("C. 前端源码静态不变量")
print("=" * 72)

page_s = io.open(os.path.join(ROOT, "frontend", "src", "pages", "CommentsPage.tsx"),
                 encoding="utf-8").read()
hook_s = io.open(os.path.join(ROOT, "frontend", "src", "hooks", "useComments.ts"),
                 encoding="utf-8").read()
api_s = io.open(os.path.join(ROOT, "frontend", "src", "lib", "api.ts"),
                encoding="utf-8").read()

chk("CommentsPage 不再写死 limit: 300", "limit: 300" not in page_s)
chk("CommentsPage 有 offset 状态", "const [offset, setOffset] = useState(0)" in page_s)
chk("CommentsPage 每页条数候选 50/100/200",
    "const PAGE_SIZES = [50, 100, 200]" in page_s)
chk("params 同时带 limit 与 offset", "\n    limit, offset,\n" in page_s)
chk("params 依赖数组含 limit/offset", "hideOwn, limit, offset])" in page_s)
chk("筛选变化回到第 1 页", "setCheckedIds(new Set()); setOffset(0)" in page_s)
chk("末页删空自动回退", "Math.floor((total - 1) / limit) * limit" in page_s)
chk("有上一页按钮", "上一页" in page_s and "ChevronLeft" in page_s)
chk("有下一页按钮", "下一页" in page_s and "ChevronRight" in page_s)
chk("有「第 X-Y 条 / 共 Z 条」", "条 / 共 {total} 条" in page_s)
chk("有每页条数选择器", "条/页" in page_s)
chk("有页码「第 N / M 页」", "Math.floor(offset / limit) + 1" in page_s)
chk("条数偏好持久化", 'localStorage.setItem("sphgj.comments_page_size"' in page_s)
chk("分页条只在 total>0 时显示", "{total > 0 && (" in page_s)

chk("hook 默认每页 50", "params.limit ?? 50" in hook_s)
chk("hook 初始 offset 跟随 params", "offset: params.offset ?? 0" in hook_s)
chk("hook 判定是否停在第一页", "onFirstPage" in hook_s)
chk("hook 非首页只更新 total(items 不动)",
    "items: onFirstPage" in hook_s and ": prev.items," in hook_s)
chk("hook 空批次直接返回(不制造空 setPage)",
    "if (!incoming?.length) return" in hook_s)
chk("hook 返回值带 limit/offset", "limit: page.limit," in hook_s)

chk("api.getComments 支持 limit/offset",
    "limit?: number; offset?: number" in api_s)
chk("CommentPage 类型含 limit/offset", "limit: number\n  offset: number" in api_s)

# ===================================================================== D 构建产物
print()
print("=" * 72)
print("D. 前端构建产物(frontend/dist 必须是最新构建)")
print("=" * 72)

dist = os.path.join(ROOT, "frontend", "dist")
idx = io.open(os.path.join(dist, "index.html"), encoding="utf-8").read()
m = re.search(r'/assets/(index-[A-Za-z0-9_\-]+\.js)', idx)
chk("dist/index.html 引用了 js 产物", bool(m), idx[:160])
js_path = os.path.join(dist, "assets", m.group(1)) if m else None
chk("引用的 js 文件存在", bool(js_path and os.path.exists(js_path)), str(js_path))
if js_path and os.path.exists(js_path):
    js = io.open(js_path, encoding="utf-8", errors="replace").read()
    chk("产物里没有旧的 limit:300", "limit:300" not in js)
    chk("产物里有分页文案「条/页」", "条/页" in js)
    chk("产物里有翻页文案「上一页」", "上一页" in js)
    chk("产物里有「已选」批量文案(批量删除仍可用)", "批量删除" in js)
    chk("产物里有隐藏自己评论的开关", "隐藏发出的评论" in js)

# ===================================================================== 收尾
try:
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)
except Exception:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 72)
print("结果: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(1 if FAIL else 0)
