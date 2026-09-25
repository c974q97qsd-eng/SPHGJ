# -*- coding: utf-8 -*-
"""作品抓取翻页/早停 回归测试(不启动浏览器,用假 page/context 驱动 PostFetcher.refresh)。

覆盖曾经的真实故障:
  post_fetcher.refresh 曾在 upsert_posts 之后判定「整页已入库且 stat_sig 未变」,
  刚写入的行必然命中 -> 每个账号永远只抓第 1 页(pageSize=200),作品永远只有 200 条。
现在的规则:
  a) 本地未全量过(full_synced=0) -> 禁止早停,一路翻到底把作品补全
  b) 已全量过 + 本页 id 全部已知 -> 早停(其后都是更老的作品)
  c) 翻到空页 / 不足一页 -> 标记已全量
  d) max_pages 截断 -> 不标记已全量,下次继续补

跑法: 配置环境安装\\Python314\\python.exe tools/test_posts_paging.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import Storage          # noqa: E402
from backend import post_fetcher as pf       # noqa: E402

PASS = 0
FAIL = 0


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (name, extra))


def make_post(i, oid=None):
    """第 i 个作品(0 最新)。字段名与官方 post_list 返回一致。"""
    oid = oid or ("oid_%04d" % i)
    return {
        "objectId": oid,
        "exportId": "exp_" + oid,
        "desc": {"description": "作品 %d" % i,
                 "media": [{"coverUrl": "https://cdn.example.com/c%d.jpg" % i}]},
        "createTime": 1750000000 - i * 3600,
        "readCount": 1000 + i, "likeCount": 10 + i, "commentCount": i,
        "forwardCount": 1, "favCount": 2, "followCount": 3,
        "visibleType": 1, "stickyOpStatus": 0,
    }


class FakePage:
    def __init__(self, total, new_count=0):
        self.total = total
        self.new_count = new_count   # 列表最前面 new_count 条是"新发作品"(全新 id)
        self.calls = []          # 记录每次请求的 currentPage

    async def goto(self, *a, **k):
        return None

    async def wait_for_timeout(self, *a, **k):
        return None

    async def close(self):
        return None

    async def evaluate(self, js, arg):
        cur = int(arg["body"]["currentPage"])
        ps = int(arg["body"]["pageSize"])
        self.calls.append(cur)
        out = []
        for k in range((cur - 1) * ps, min(cur * ps, self.total)):
            # 位置 k:前 new_count 条是新作品(全新 id),其后是已存在的老作品
            if k < self.new_count:
                out.append(make_post(k, "oid_new_%04d" % k))
            else:
                out.append(make_post(k, "oid_%04d" % (k - self.new_count)))
        return {"status": 200, "body": json.dumps({"errCode": 0, "data": {"list": out}})}


class FakeContext:
    def __init__(self, page):
        self._p = page

    async def new_page(self):
        return self._p


class FakeWorker:
    def __init__(self, ctx, account):
        self.context = ctx
        self.account = account


async def no_covers(self, context, rows):
    """跳过封面下载(测试只关心翻页与入库)。"""
    return 0


def run_refresh(storage, total, account, meta_kw=None, max_pages=20, seed=None, new_count=0):
    """跑一次 refresh。seed=前 N 个作品预置入库(模拟存量数据)。"""
    account = dict(account)
    if seed is not None:
        rows = [pf._extract_post(make_post(i)) for i in range(seed)]
        for r in rows:
            r["cover_path"] = ""
        storage.upsert_posts(account["id"], rows)
    if meta_kw:
        storage.set_post_fetch_meta(account["id"], **meta_kw)
    page = FakePage(total, new_count=new_count)
    worker = FakeWorker(FakeContext(page), account)
    cfg = {"posts": {"page_size": 200, "max_pages": max_pages, "refresh_cooldown_sec": 0}}
    f = pf.PostFetcher(storage, account, cfg)
    res = asyncio.get_event_loop().run_until_complete(f.refresh(worker))
    n = storage._conn().execute(
        "SELECT COUNT(*) FROM posts WHERE account_id=?", (account["id"],)).fetchone()[0]
    return res, n, page.calls


def main():
    tmp = tempfile.mkdtemp(prefix="sphgj_paging_")
    try:
        pf.PostFetcher._ensure_covers = no_covers
        acc = {"id": "acc_a", "name": "测试号", "_aid": "aid-x", "_log_finder_id": "fid-x"}
        storage = Storage(os.path.join(tmp, "t.db"))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        print("== 1. 首次抓取必须翻到底(450 条 -> 3 页) ==")
        res, n, calls = run_refresh(storage, 450, acc)
        chk("pages == 3", res.get("pages") == 3, res)
        chk("requests 顺序 1,2,3", calls == [1, 2, 3], calls)
        chk("入库 450 条", n == 450, n)
        chk("full == True", res.get("full") is True, res)
        chk("落库 full_synced=1", storage.get_post_fetch_meta("acc_a").get("full_synced") == 1)
        chk("未早停", res.get("early_stop") is False, res)

        print("== 2. 已全量 + 无变化 -> 第 1 页即早停 ==")
        storage.set_post_fetch_meta("acc_a", last_refresh=None)
        res, n, calls = run_refresh(storage, 450, acc, meta_kw={"full_synced": 1})
        chk("pages == 1", res.get("pages") == 1, res)
        chk("early_stop == True", res.get("early_stop") is True, res)
        chk("只请求第 1 页", calls == [1], calls)

        print("== 3. 已全量 + 新增 10 条 -> 追到第 2 页停 ==")
        res, n, calls = run_refresh(storage, 460, acc, meta_kw={"full_synced": 1}, new_count=10)
        chk("pages == 2", res.get("pages") == 2, res)
        chk("early_stop == True", res.get("early_stop") is True, res)
        chk("入库补到 460", n == 460, n)
        chk("新作品已落库", storage._conn().execute(
            "SELECT COUNT(*) FROM posts WHERE account_id='acc_a' AND object_id LIKE 'oid_new_%'"
        ).fetchone()[0] == 10)

        print("== 4. 【旧故障回归】本地已有 200 条但从未全量过 -> 不得早停,继续翻到底 ==")
        storage2 = Storage(os.path.join(tmp, "t2.db"))
        res, n, calls = run_refresh(storage2, 450, acc, seed=200)
        chk("pages == 3(没有被第 1 页早停卡住)", res.get("pages") == 3, res)
        chk("入库 450 条(不再是 200)", n == 450, n)
        chk("requests 顺序 1,2,3", calls == [1, 2, 3], calls)
        chk("full == True", res.get("full") is True, res)

        print("== 5. max_pages 截断 -> 不算全量,下次继续 ==")
        storage3 = Storage(os.path.join(tmp, "t3.db"))
        res, n, calls = run_refresh(storage3, 450, acc, max_pages=2)
        chk("pages == 2", res.get("pages") == 2, res)
        chk("full == False", res.get("full") is False, res)
        chk("full_synced 仍为 0", storage3.get_post_fetch_meta("acc_a").get("full_synced") == 0)
        print("     -- 第二次刷新(库已有 400 条,仍未全量)应继续也翻满 2 页 --")
        storage3.set_post_fetch_meta("acc_a", last_refresh=None)
        res, n, calls = run_refresh(storage3, 450, acc, max_pages=2)
        chk("pages == 2(未早停)", res.get("pages") == 2, res)
        chk("full_synced 仍为 0", storage3.get_post_fetch_meta("acc_a").get("full_synced") == 0)

        print("== 6. 作品数 < 一页(30 条) -> 一次到底并标记全量 ==")
        storage4 = Storage(os.path.join(tmp, "t4.db"))
        res, n, calls = run_refresh(storage4, 30, acc)
        chk("pages == 1", res.get("pages") == 1, res)
        chk("full == True", res.get("full") is True, res)
        chk("入库 30", n == 30, n)

        print("== 7. 冷却期跳过 ==")
        storage5 = Storage(os.path.join(tmp, "t5.db"))
        from datetime import datetime
        storage5.set_post_fetch_meta("acc_a", last_refresh=datetime.now().isoformat(), last_pages=1)
        page = FakePage(450)
        worker = FakeWorker(FakeContext(page), acc)
        cfg = {"posts": {"page_size": 200, "max_pages": 20, "refresh_cooldown_sec": 60}}
        r = asyncio.get_event_loop().run_until_complete(
            pf.PostFetcher(storage5, acc, cfg).refresh(worker))
        chk("skipped 冷却中", bool(r.get("skipped")), r)
        chk("零请求", page.calls == [], page.calls)

        print("== 8. storage.posts_all_known 语义 ==")
        ids = ["oid_0000", "oid_0001"]
        chk("全部已知 -> True", storage.posts_all_known("acc_a", ids) is True)
        chk("含未知 id -> False", storage.posts_all_known("acc_a", ids + ["nope"]) is False)
        chk("空列表 -> False", storage.posts_all_known("acc_a", []) is False)

        print("\n%s  %d/%d" % ("ALL PASS" if FAIL == 0 else "HAS FAILURE", PASS, PASS + FAIL))
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
