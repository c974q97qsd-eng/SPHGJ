"""验证请求量最大的两个优化方向。

T1. onlyUnread=True 是否只返回"有未读评论"的视频 —— 若是,遍历全量视频就没必要了
T2. pageSize=100 的分页是否正确(lastBuff 翻页不重复、不遗漏)
T3. comment_list 的排序规则(决定能否"翻到全是已知评论即停")
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"D:\SPHGJ")

from backend import wx_http  # noqa: E402

BASE = "https://channels.weixin.qq.com/micro/interaction/cgi-bin/mmfinderassistant-bin"
PAGE_URL = "https://channels.weixin.qq.com/micro/interaction/comment"
CFG = r"\\Sphgj\sphgj\config.json"


def pick():
    cfg = json.load(open(CFG, encoding="utf-8"))
    for a in cfg.get("accounts") or []:
        ss = a.get("storage_state") or {}
        if wx_http.build_cookie_header(ss) and a.get("_aid") and a.get("_log_finder_id"):
            return a
    return None


async def post(acc, path, **kw):
    aid, fid = acc["_aid"], acc["_log_finder_id"]
    url = f"{BASE}/{path}?_aid={aid}&_pageUrl={PAGE_URL}"
    t0 = time.time()
    j = await wx_http.post_json(acc, url, wx_http.std_body(fid, **kw),
                                page_url=PAGE_URL, timeout=30)
    return j, time.time() - t0


async def vids(acc, only_unread, page_size=100, last_buff=""):
    j, dt = await post(acc, "post/post_list", pageSize=page_size,
                       onlyUnread=only_unread, needAllCommentCount=True,
                       userpageType=13, forMcn=False, lastBuff=last_buff)
    d = (j or {}).get("data") or {}
    return (j.get("errCode") if isinstance(j, dict) else None), d.get("list") or [], \
        d.get("lastBuff") or "", dt


async def main():
    acc = pick()
    print(f"账号 {acc.get('id')}\n")

    print("=" * 72)
    print("T1. onlyUnread=True 行为")
    for flag in (True, True, False):
        code, lst, lb, dt = await vids(acc, flag, page_size=100)
        ccs = [int(v.get("commentCount") or 0) for v in lst]
        print(f"  onlyUnread={str(flag):5s} -> {len(lst):3d} 条 | errCode={code} "
              f"| {dt*1000:.0f}ms | lastBuff={bool(lb)} | commentCount 和={sum(ccs)}")
        if flag and lst:
            for v in lst[:5]:
                print(f"      {v.get('objectId','')[:28]} cc={v.get('commentCount')} "
                      f"desc={str(v.get('description'))[:16]}")
    print("  注:若 onlyUnread=True 条数远小于 False,说明它只返回有未读评论的视频")

    print()
    print("=" * 72)
    print("T2. pageSize=100 分页正确性(取前 3 页,查重复/排序)")
    seen, lb = [], ""
    for p in range(3):
        code, lst, lb, dt = await vids(acc, False, page_size=100, last_buff=lb)
        ids = [v.get("objectId") for v in lst]
        dup = len(ids) - len(set(ids))
        overlap = len(set(ids) & set(seen))
        ts = [int(v.get("createTime", 0) or 0) for v in lst]
        print(f"  第{p+1}页: {len(lst):3d} 条 | 页内重复={dup} | 与前页重复={overlap} "
              f"| 时间 {time.strftime('%m-%d %H:%M', time.localtime(ts[0]))} ~ "
              f"{time.strftime('%m-%d %H:%M', time.localtime(ts[-1]))}" if ts else "空")
        seen += ids
        if not lb or not lst:
            print("  已到末页")
            break
        await asyncio.sleep(1.2)

    print()
    print("=" * 72)
    print("T3. comment_list 排序规则")
    code, lst, _, _ = await vids(acc, False, page_size=100)
    cands = sorted([v for v in lst if int(v.get("commentCount") or 0) >= 5],
                   key=lambda v: -int(v.get("commentCount") or 0))[:2]
    if not cands:
        print("  前 100 个视频没有 >=5 条评论的,略过")
        return
    for v in cands:
        oid, cc = v.get("objectId"), v.get("commentCount")
        print(f"\n  视频 {oid[:36]} commentCount={cc}")
        lb = ""
        for p in range(2):
            j, dt = await post(acc, "comment/comment_list", lastBuff=lb, exportId=oid,
                               commentSelection=False, forMcn=False)
            cms = ((j or {}).get("data") or {}).get("comment") or []
            lb = ((j or {}).get("data") or {}).get("lastBuff") or ""
            ts = [int(c.get("commentCreatetime", 0) or 0) for c in cms]
            if not ts:
                print(f"    第{p+1}页: 空")
                break
            order = "倒序" if all(ts[i] >= ts[i+1] for i in range(len(ts)-1)) else \
                    ("正序" if all(ts[i] <= ts[i+1] for i in range(len(ts)-1)) else "无序")
            print(f"    第{p+1}页: {len(cms):3d} 条 | {order} | "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(ts[0]))} ~ "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(ts[-1]))} | "
                  f"next={bool(lb)}")
            if not lb:
                break
            await asyncio.sleep(1.2)


asyncio.run(main())
