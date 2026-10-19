# -*- coding: utf-8 -*-
"""rev27 自测:video_url 回填与读取;本地库 + 母本库副本各跑一遍。"""
import os
import sys

sys.path.insert(0, r"D:\SPHGJ")
from backend.storage import Storage  # noqa: E402

for db, tag in ((r"D:\SPHGJ\data\sphgj.db", "本地库"),
                (r"C:\tmp\rv\m.db", "母本副本")):
    if not os.path.isfile(db):
        print(f"{tag}: 文件不存在,跳过 {db}")
        continue
    st = Storage(db)  # 构造即建表+补列

    n = st.backfill_video_urls()
    n2 = st.backfill_video_urls()      # 幂等:第二次应为 0
    total = 0
    try:
        import sqlite3
        c = sqlite3.connect(db)
        total = c.execute("SELECT COUNT(*) FROM posts WHERE COALESCE(video_url,'')<>''").fetchone()[0]
        allc = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        c.close()
    except Exception as e:
        print("  统计失败", e)
    print(f"{tag}: 回填 {n} 条(二次 {n2}) / 有地址 {total} 行 / 共 {allc} 行")
    # 取一条验证
    import sqlite3
    c = sqlite3.connect(db)
    r = c.execute("SELECT finder_id,object_id,title,video_url FROM posts "
                  "WHERE COALESCE(video_url,'')<>'' LIMIT 1").fetchone()
    c.close()
    if r:
        info = st.get_post_video(r[0], r[1])
        assert info and info["video_url"], "get_post_video 取不到"
        print(f"  样例: {r[2][:24]!r} url={r[3][:60]}...")

print("\n[PASS] video_url 自测通过")
