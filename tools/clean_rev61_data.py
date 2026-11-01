# -*- coding: utf-8 -*-
"""rev61 数据修正：
  1) own_comments 里 3 条「话术其实是自动评论」的记录，via 由 manual_reply 改回 auto_comment
     （不删行 —— comment_id 上有 UNIQUE，删了这两个评论就从「自己发的评论」里彻底消失，
       隐藏功能会漏掉它们）
  2) own_comment_pending 里 6 条已过期、新时间窗下永远补不上的待确认记录清掉

先备份整库，再事务执行，前后各打一遍。
"""
import os
import sqlite3
import shutil
import time

REMOTE = r"\\Sphgj\sphgj\data\sphgj.db"
LOCAL_BAK = r"D:\SPHGJ\data\backup-rev61-%s.db" % time.strftime("%Y%m%d-%H%M%S")

# 3 条：内容都是自动评论话术「点头像，进入直播间领取优惠券下单！」
WRONG_VIA = ("14772638802065954976", "14772636205262051498", "14772638920156780702")
PENDING_IDS = (3, 4, 5, 6, 8, 9)

print("[1/3] 备份整库 ->", LOCAL_BAK)
os.makedirs(os.path.dirname(LOCAL_BAK), exist_ok=True)
shutil.copy2(REMOTE, LOCAL_BAK)
print("      备份完成 %.1f MB" % (os.path.getsize(LOCAL_BAK) / 1048576.0))

c = sqlite3.connect(REMOTE, timeout=20)
try:
    print("[2/3] 修正前")
    print("      own_comments:", c.execute(
        "SELECT via, COUNT(*) FROM own_comments GROUP BY via").fetchall())
    print("      pending ids:", [r[0] for r in c.execute(
        "SELECT id FROM own_comment_pending ORDER BY id")])

    q1 = ("UPDATE own_comments SET via='auto_comment' "
          "WHERE via='manual_reply' AND comment_id IN (%s)" % ",".join("?" * len(WRONG_VIA)))
    n1 = c.execute(q1, WRONG_VIA).rowcount

    q2 = ("DELETE FROM own_comment_pending WHERE id IN (%s)"
          % ",".join("?" * len(PENDING_IDS)))
    n2 = c.execute(q2, PENDING_IDS).rowcount
    c.commit()

    print("[3/3] 已提交: via 修正 %d 行, 待确认删除 %d 行" % (n1, n2))
    print("      own_comments:", c.execute(
        "SELECT via, COUNT(*) FROM own_comments GROUP BY via").fetchall())
    print("      manual_reply 剩余:", c.execute(
        "SELECT comment_id, created_at FROM own_comments WHERE via='manual_reply'").fetchall())
    print("      pending 剩余:", [r[0] for r in c.execute(
        "SELECT id FROM own_comment_pending ORDER BY id")])
finally:
    c.close()
