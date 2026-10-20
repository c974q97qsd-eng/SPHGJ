# -*- coding: utf-8 -*-
"""升级干跑:在【库副本】上把老库完整升级一遍,看老数据到底会变成什么样。

用法(项目根目录):
    python tools/dryrun_upgrade.py <老库路径> [config.json] [--keep]

做的事(全程在临时副本上,生产库只读一次):
  1. 复制库(+ -wal/-shm)到临时目录
  2. 用当前 backend.storage.Storage 打开 -> 自动跑 _init 结构迁移
     (补 finder_id 列 / 重建 video_stats、auto_commented 主键 / 补 video_url 等)
  3. 按 config.json 的映射跑 migrate_posts_to_finder + migrate_comments_to_finder
  4. 输出:迁移前后行数、兜底行、重复行、以及「哪一步会掉数据」
  5. 默认删除临时副本(--keep 保留,便于自己再查)
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TABLES = ("posts", "comments", "video_stats", "auto_commented",
          "own_comments", "own_comment_pending", "delete_logs")


def snap(db):
    """快照:每表行数 + 兜底行数 + 视频号分布。"""
    c = sqlite3.connect(db)
    out = {}
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in TABLES:
            if t not in tables:
                out[t] = None
                continue
            cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
            n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            d = {"rows": n, "finder": bool("finder_id" in cols and "finder_id" in cols)}
            if "finder_id" in cols:
                d["fb"] = c.execute(
                    "SELECT COUNT(*) FROM %s WHERE finder_id LIKE 'acct:%%'" % t).fetchone()[0]
                d["empty"] = c.execute(
                    "SELECT COUNT(*) FROM %s WHERE COALESCE(finder_id,'')=''" % t).fetchone()[0]
                d["fids"] = c.execute(
                    "SELECT COUNT(DISTINCT finder_id) FROM %s WHERE COALESCE(finder_id,'')<>''" % t).fetchone()[0]
            out[t] = d
        return out
    finally:
        c.close()


def dup_report(db):
    """迁移后重复检查:同一作品挂在多个视频号 / 同一评论 id 多行。"""
    c = sqlite3.connect(db)
    msgs = []
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "posts" in tables:
            colsc = [r[1] for r in c.execute("PRAGMA table_info(posts)")]
            if "finder_id" in colsc:
                n = c.execute("""SELECT COUNT(*) FROM (SELECT object_id FROM posts
                                 GROUP BY object_id HAVING COUNT(DISTINCT finder_id)>1)""").fetchone()[0]
                msgs.append(("同一作品挂在多个视频号", n))
        if "comments" in tables:
            colsc = [r[1] for r in c.execute("PRAGMA table_info(comments)")]
            if "finder_id" in colsc:
                n = c.execute("""SELECT COUNT(*) FROM (SELECT comment_id FROM comments
                                 GROUP BY comment_id HAVING COUNT(*)>1)""").fetchone()[0]
                msgs.append(("同一评论 id 多行", n))
        if "video_stats" in tables:
            colsc = [r[1] for r in c.execute("PRAGMA table_info(video_stats)")]
            if "finder_id" in colsc:
                n = c.execute("""SELECT COUNT(*) FROM (SELECT finder_id,export_id FROM video_stats
                                 GROUP BY 1,2 HAVING COUNT(*)>1)""").fetchone()[0]
                msgs.append(("video_stats 主键重复", n))
        return msgs
    finally:
        c.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    cfg_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") \
        else os.path.join(ROOT, "config.json")
    keep = "--keep" in sys.argv
    if not os.path.isfile(src):
        print("[ERROR] 库不存在: %s" % src)
        return 2

    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.isfile(cfg_path) else {}
    fid_map = {}
    for a in cfg.get("accounts", []):
        fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or a.get("finder_id") or "").strip()
        if fid and a.get("id"):
            fid_map[a["id"]] = fid

    tmp = tempfile.mkdtemp(prefix="sphgj_dry_")
    db = os.path.join(tmp, "dry.db")
    shutil.copy2(src, db)
    for ext in ("-wal", "-shm"):
        if os.path.exists(src + ext):
            shutil.copy2(src + ext, db + ext)

    print("=" * 78)
    print("SPHGJ 升级干跑(副本: %s)" % db)
    print("  源库 : %s (%.1f MB)" % (src, os.path.getsize(src) / 1048576.0))
    print("  映射 : %d 个账号" % len(fid_map))
    before = snap(db)

    try:
        from backend.storage import Storage
        st = Storage(db)                          # 这一步就会跑 _init 结构迁移
        print("[1] _init 结构迁移完成 (补列 / 重建主键 / 建新表)")
        r1 = st.migrate_posts_to_finder(fid_map, covers_root=os.path.join(tmp, "covers"))
        print("[2] 作品归属迁移: 作品 %d 条 / 账本 %d 条 / 封面目录 %d 个 / 未识别卡片 %s"
              % (r1["posts"], r1["meta"], r1["covers"], r1["unmapped"] or "无"))
        r2 = st.migrate_comments_to_finder(fid_map)
        print("[3] 评论归属迁移: %s / 合并重复 %d 行" % (r2["tables"], r2["merged"]))
        nv = st.backfill_video_urls()
        print("[4] 回填作品播放地址: %d 条" % nv)
    finally:
        pass

    after = snap(db)
    print("-" * 78)
    print("[5] 前后对比")
    print("  %-20s %10s %10s %10s %10s %8s" % ("表", "迁移前", "迁移后", "兜底行", "空归属", "视频号"))
    for t in TABLES:
        b, a = before.get(t), after.get(t)
        if not a:
            print("  %-20s %10s %10s" % (t, "-" if not b else b["rows"], "无此表"))
            continue
        print("  %-20s %10s %10d %10s %10s %8s" % (
            t, "-" if not b else b["rows"], a["rows"],
            a.get("fb", "-"), a.get("empty", "-"), a.get("fids", "-")))

    lost = [(t, before[t]["rows"], after[t]["rows"]) for t in TABLES
            if before.get(t) and after.get(t) and after[t]["rows"] < before[t]["rows"]]
    print("-" * 78)
    print("[6] 完整性检查")
    if lost:
        for t, b, a in lost:
            print("  !! %s 行数变少: %d -> %d (差 %d)" % (t, b, a, b - a))
        print("     (video_stats / auto_commented 是主键重建去重,变少属正常;其它表变少要查)")
    else:
        print("  所有表行数未减少")
    for name, n in dup_report(db):
        print("  %s: %d 处%s" % (name, n, "" if n == 0 else "  <- 需要处理"))
    fb_total = sum((after[t] or {}).get("fb", 0) or 0 for t in TABLES)
    print("  兜底行合计: %d" % fb_total)

    print("=" * 78)
    if fb_total == 0:
        print("[结论] 干跑通过:老数据全部归到真实视频号,可以按此配置升级。")
    else:
        print("[结论] 有 %d 行会落 acct: 兜底,先补齐 config 映射再升级(见 upgrade_check.py 建议)。" % fb_total)
    print("=" * 78)
    if keep:
        print("副本保留在: %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
