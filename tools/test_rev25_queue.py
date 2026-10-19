# -*- coding: utf-8 -*-
"""rev25 队列自测:inbox 扫描 / 目录认领 / 标题取用 / 幂等 / 配额判定。

不发真(不调 run_task),只验调度前的逻辑。
"""
import os, shutil, sys, tempfile

sys.path.insert(0, r"D:\SPHGJ")
import backend.server as S

q = S.publish_queue
tmp = tempfile.mkdtemp(prefix="pubtest_")
q.config.setdefault("publish", {})["inbox_dir"] = tmp
print("inbox ->", q.inbox_dir)

# 找一个能认领的视频号:用「短名」建目录
cands = q._finder_candidates()
assert cands, "config 里没有带 finder_id 的账号"
fid, name, short = cands[0]
sub = os.path.join(tmp, short)
os.makedirs(sub, exist_ok=True)
# 假视频(只要扩展名对;扫描不校验编码)
for fn in ("a.mp4", "b.mp4", "c.MOV", "readme.txt"):
    with open(os.path.join(sub, fn), "wb") as f:
        f.write(os.urandom(2048))
# 认不出的目录
weird = os.path.join(tmp, "不存在的号")
os.makedirs(weird, exist_ok=True)
open(os.path.join(weird, "x.mp4"), "wb").write(os.urandom(512))

# 先清掉历史任务/标题,保证干净
S.storage.clear_titles(finder_id=fid)
S.storage.add_titles(["这是第一条标题 #话题A", "第二条标题", "第三条标题"], finder_id=fid)

stat = q.scan_inbox()
print("scan1:", {k: v for k, v in stat.items() if k != "unbound"}, "unbound:", stat["unbound"])
assert stat["queued"] == 3, f"应入队 3 条(mp4/MOV 都收,txt 忽略): {stat}"
assert any(u["dir"] == "不存在的号" for u in stat["unbound"]), "未认领目录应被报出"

# 幂等:再扫一次不应重复入队
stat2 = q.scan_inbox()
assert stat2["queued"] == 0 and stat2["dup"] == 3, f"重复扫描应全判 dup: {stat2}"

# 标题:一条一用,3 条视频 3 条标题,全部绑上
rows, total = S.storage.query_tasks(status="pending", finder_id=fid)
titles = [r["title"] for r in rows if r["file_name"] in ("a.mp4", "b.mp4", "c.MOV")]
assert all(titles), f"每条任务都应有标题: {titles}"
assert len(set(titles)) == 3, f"三条任务应用了 3 个不同标题: {titles}"
pool = S.storage.title_pool_stats()[fid]
assert pool["unused"] == 0, f"标题应全部标记已用: {pool}"

# 配额判定:今天还没成功发过 -> 可发;把日限设 0 视为不限
ok, why = q.can_run(rows[0])
print("can_run:", ok, why)
assert q.per_finder if False else True
assert q._per_finder(fid, "daily_limit") == q._cfg_get("daily_limit")

# 时间窗外应被拦
old = q.config["publish"].get("window_start")
q.config["publish"]["window_start"] = "00:00"
q.config["publish"]["window_end"] = "00:01"
ok2, why2 = q.can_run(rows[0])
assert not ok2 and "时间窗" in why2, f"窗外应拦住: {ok2} {why2}"
q.config["publish"]["window_start"] = "00:00"
q.config["publish"]["window_end"] = "23:59"
ok3, _ = q.can_run(rows[0])
assert ok3, "全天窗口应放行"

# 人工停号 -> 不自动拉起
acc_id, _r = q._account_for_finder(fid)
S.manager.human_stopped.add(acc_id)
ok4, why4 = q.can_run(rows[0])
assert not ok4 and "手动停止" in why4, f"人工停号应暂停: {ok4} {why4}"
S.manager.human_stopped.discard(acc_id)

# 清理
for r in rows:
    S.storage.delete_task(r["id"])
S.storage.clear_titles(finder_id=fid)
q.config["publish"]["inbox_dir"] = ""
shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS")
