# -*- coding: utf-8 -*-
"""升级体检:老库升级到当前版本【前 / 后】各跑一次,只读,绝不动原库。

用法(在项目根目录):
    python tools/upgrade_check.py                    # 用 config.json 里的 db_path
    python tools/upgrade_check.py <db> [config.json] # 指定库 / 配置

做什么:
  1. 先把库(含 -wal / -shm)整体复制到临时目录,所有查询都在副本上做,生产库零风险
  2. 判定库的「代际」:v22 及以前(account_id 主键) / v23+(finder_id 归属)
  3. 统计 8 张归属表的行数 + acct: 兜底行数
  4. 用 config.json 预演迁移:多少行能映射到真视频号、多少会掉进 acct: 兜底
  5. 给 SAFE / WARN / BLOCK 结论 + 该做什么

为什么需要它:
  老库迁移依赖 config.json 里的 _log_finder_id / locked_finder_id。缺映射的卡片
  (账号没在旧版登录过 / 卡片已被删)迁移时会落成 "acct:<卡片id>" 兜底。
  评论域每次启动都会再用配置映射刷一遍,能自愈;作品域则是一次性迁移,
  首启没映射上就不会再自动纠正 —— 所以【升级前】必须先把映射确认齐。
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
COMMENT_TABLES = ("comments", "video_stats", "auto_commented",
                  "own_comments", "own_comment_pending", "delete_logs")
ALL_TABLES = ("posts",) + COMMENT_TABLES


def cols(c, t):
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
    except sqlite3.Error:
        return []


def one(c, sql, args=()):
    try:
        r = c.execute(sql, args).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def load_fid_map(cfg_path):
    """从 config.json 取 {account_id: finder_id}。取不到映射的卡片单独列出来。"""
    try:
        cfg = json.load(open(cfg_path, encoding="utf-8"))
    except Exception as e:
        return None, [], str(e)
    fid_map, missing = {}, []
    for a in cfg.get("accounts", []):
        aid = (a.get("id") or "").strip()
        if not aid:
            continue
        fid = (a.get("_log_finder_id") or a.get("locked_finder_id") or a.get("finder_id") or "").strip()
        if fid:
            fid_map[aid] = fid
        else:
            missing.append((aid, a.get("name") or ""))
    return fid_map, missing, ""


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "config.json")
    if not db:
        try:
            _c = json.load(open(cfg, encoding="utf-8"))
        except Exception:
            _c = {}
        db = os.path.join(ROOT, (_c.get("db_path") or "./data/sphgj.db").lstrip("./"))
    if not os.path.isfile(db):
        print("[ERROR] 库不存在: %s" % db)
        return 2

    print("=" * 78)
    print("SPHGJ 升级体检")
    print("  库文件   : %s (%.1f MB)" % (db, os.path.getsize(db) / 1048576.0))
    print("  配置文件 : %s%s" % (cfg, "" if os.path.isfile(cfg) else "  [不存在]"))
    for ext in ("-wal", "-shm"):
        p = db + ext
        if os.path.exists(p):
            print("  附带文件 : %s (%.1f MB)" % (ext, os.path.getsize(p) / 1048576.0))

    # ---- 复制到临时目录,只查副本 ----
    tmp = tempfile.mkdtemp(prefix="sphgj_chk_")
    dst = os.path.join(tmp, "chk.db")
    try:
        shutil.copy2(db, dst)
        for ext in ("-wal", "-shm"):
            if os.path.exists(db + ext):
                shutil.copy2(db + ext, dst + ext)
        c = sqlite3.connect(dst)
        c.execute("PRAGMA busy_timeout=8000")

        # ---- 1. 代际判定 ----
        pcols, ccols = cols(c, "posts"), cols(c, "comments")
        has_posts = bool(pcols)
        posts_new = "finder_id" in pcols
        comments_new = "finder_id" in ccols
        vs_new = "finder_id" in cols(c, "video_stats")
        has_pub = bool(cols(c, "publish_tasks"))
        has_vurl = "video_url" in pcols
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        print("-" * 78)
        print("[1] 库结构代际")
        print("  posts          : %s" % ("不存在" if not has_posts else
                                        ("v23+ 已按 finder_id 归属" if posts_new else "v22 及以前(account_id 主键)")))
        print("  comments       : %s" % ("不存在" if not ccols else
                                        ("v24+ 已按 finder_id 归属" if comments_new else "v22 及以前(无 finder_id 列)")))
        print("  video_stats    : %s" % ("v24+ 已按 finder_id 归属" if vs_new else "v22 及以前(account_id+export_id 主键)"))
        print("  publish_tasks  : %s (rev25 自动发作品)" % ("有" if has_pub else "无,升级时会新建"))
        print("  posts.video_url: %s (rev27 作品播放)" % ("有" if has_vurl else "无,升级时 ALTER 补列"))
        gen_new = posts_new and comments_new and vs_new
        print("  => 结论: %s" % ("已是新版结构,本次只会补缺列/补表" if gen_new else "老结构,升级时会做一次性迁移"))

        # ---- 2. 行数 / 兜底 ----
        print("-" * 78)
        print("[2] 数据量与兜底行(acct:<卡片id>)")
        print("  %-20s %10s %10s" % ("表", "行数", "acct:兜底"))
        total_rows = 0
        for t in ALL_TABLES:
            if t not in tables:
                print("  %-20s %10s" % (t, "无此表"))
                continue
            n = one(c, "SELECT COUNT(*) FROM %s" % t) or 0
            total_rows += n
            if "finder_id" in cols(c, t):
                fb = one(c, "SELECT COUNT(*) FROM %s WHERE finder_id LIKE 'acct:%%'" % t) or 0
                emp = one(c, "SELECT COUNT(*) FROM %s WHERE COALESCE(finder_id,'')=''" % t) or 0
                print("  %-20s %10d %10d%s" % (t, n, fb, ("  (空归属 %d)" % emp) if emp else ""))
            else:
                print("  %-20s %10d %10s" % (t, n, "待迁移"))
        print("  %-20s %10d" % ("合计", total_rows))

        # ---- 3. 配置映射 ----
        fid_map, missing, err = load_fid_map(cfg)
        print("-" * 78)
        print("[3] 配置里的账号 -> 视频号映射")
        if fid_map is None:
            print("  [ERROR] 读不了配置: %s" % err)
            fid_map, missing = {}, []
        print("  有映射 %d 个, 缺映射 %d 个" % (len(fid_map), len(missing)))
        for aid, fid in fid_map.items():
            print("    OK   %-16s -> %s" % (aid, fid))
        for aid, name in missing:
            print("    MISS %-16s (%s)  <- 迁移会落 acct:%s" % (aid, name, aid))

        # ---- 4. 迁移预演 ----
        print("-" * 78)
        print("[4] 迁移预演(按 account_id 分组,看能不能映射到真视频号)")
        will_ok = will_fb = 0
        bad_cards = {}
        for t in ALL_TABLES:
            if t not in tables:
                continue
            tcols = cols(c, t)
            if "account_id" not in tcols:
                continue
            # 已迁过的行(finder 真值)不用再算
            where = "WHERE 1=1"
            if "finder_id" in tcols:
                where += " AND (COALESCE(finder_id,'')='' OR finder_id LIKE 'acct:%')"
            try:
                rows = c.execute("SELECT COALESCE(account_id,''), COUNT(*) FROM %s %s "
                                 "GROUP BY 1 ORDER BY 2 DESC" % (t, where)).fetchall()
            except sqlite3.Error:
                continue
            for acc, n in rows:
                if acc and acc in fid_map:
                    will_ok += n
                else:
                    will_fb += n
                    k = acc or "(空 account_id)"
                    bad_cards[k] = bad_cards.get(k, 0) + n
        print("  可映射到真视频号 : %d 行" % will_ok)
        print("  会落 acct: 兜底   : %d 行" % will_fb)
        if bad_cards:
            print("  兜底明细(卡片 -> 行数):")
            for acc, n in sorted(bad_cards.items(), key=lambda x: -x[1]):
                why = "空ID" if acc.startswith("(") else ("配置里没有这张卡片" if acc not in [m[0] for m in missing] else "卡片存在但没抓到视频号")
                print("    %-16s %8d 行   %s" % (acc, n, why))

        # ---- 5. 结论 ----
        print("=" * 78)
        if will_fb == 0 and not bad_cards:
            print("[结论] SAFE —— 所有历史数据都能归到真实视频号,可以放心升级。")
        elif not gen_new and bad_cards:
            print("[结论] BLOCK —— 有 %d 行会落 acct: 兜底,涉及 %d 张卡片。" % (will_fb, len(bad_cards)))
            print("       升级前务必先把这些账号在【老版本】里登录一次(登录会把 _log_finder_id")
            print("       写进 config.json),或者从旧 config 备份里手工补 locked_finder_id,")
            print("       然后再启动新版 —— 作品域是一次性迁移,首启没映射上不会再自动纠正。")
        else:
            print("[结论] WARN —— 有 %d 行仍是 acct: 兜底(新版结构下可再救)。" % will_fb)
            print("       补上 config.json 的映射后,重启会用配置再刷一遍评论域;")
            print("       作品域需用 tools/fix_posts_finder.py 归位。")
        print("=" * 78)
        return 0
    finally:
        try:
            c.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
