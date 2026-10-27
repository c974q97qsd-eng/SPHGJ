# -*- coding: utf-8 -*-
"""rev56i：评论【写】通道按 platform 分流（第 2 步的关键集成点）。

问题：`POST /api/comments/{id}/reply`、`DELETE /api/comments/{id}`、
`POST /api/comments/batch-delete`、`POST /api/comments/{id}/pin` 这四条都是
视频号专用（走 `_comment_worker` + `w.api`）。抖音评论也存在同一张 comments 表里
（platform='dy'），一旦前端对着抖音评论点「回复」，会往视频号通道走 ——
最坏的结果是「提示该视频号没有登录卡片」，更坏的是把回复发到了别的号上。

本补丁做的事：
  1. storage.get_comment() 返回 platform（原来不返回，导致无法判定平台）。
  2. storage.log_delete() 支持 platform（delete_logs 表 rev56 已加 platform 列，
     但写入口一直是默认 'wx'，抖音的删除记录会被错标）。
  3. server.py 加 _comment_platform()：以**库里那一行**为权威判平台，查不到才回落。
  4. 四条路由各自加抖音分支；置顶明确回 400（抖音评论置顶接口还没探明，
     不能假装成功）。

幂等：每条 assert 命中 1 次；改完自校验 + 语法检查。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STO = os.path.join(ROOT, "backend", "storage.py")
SRV = os.path.join(ROOT, "backend", "server.py")


# 幂等保护：脚本跑过一次后，锚点必然已不存在。检测到「已应用」就直接退出，
# 免得下次误跑时抛一堆 assert（那种噪声会让人以为代码坏了）。
_MARK = os.path.join(ROOT, "backend", "server.py")
if os.path.exists(_MARK) and "def _comment_platform(" in io.open(_MARK, encoding="utf-8").read():
    print("rev56i 已应用过（backend/server.py 里已有 _comment_platform），跳过。")
    sys.exit(0)


def patch(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, "%s: 锚点命中 %d 次 tag=%s" % (os.path.basename(path), n, tag)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


# ---------------- storage ----------------
patch(STO, [
    ("get_comment", '''            r = c.execute(
                "SELECT finder_id,account_id,export_id,nickname,content FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            return dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2],
                        nickname=r[3], content=r[4])''',
     '''            r = c.execute(
                "SELECT finder_id,account_id,export_id,nickname,content,platform "
                "FROM comments WHERE comment_id=?",
                (comment_id,)).fetchone()
            if not r:
                return None
            # platform 一定要回：评论写通道要靠它分流（抖音 / 视频号），
            # 拿不到就会把抖音评论错发到视频号通道。
            return dict(finder_id=r[0] or "", account_id=r[1], export_id=r[2],
                        nickname=r[3], content=r[4], platform=(r[5] or "wx"))'''),
    ("log_delete", '''    def log_delete(self, finder_id, comment_id, nickname, content, keyword, export_id,
                   account_id=None):
        """写删除记录。归属 = finder_id(视频号),account_id 只记执行删除的卡片。"""
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                      (finder_id, account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat()))''',
     '''    def log_delete(self, finder_id, comment_id, nickname, content, keyword, export_id,
                   account_id=None, platform="wx"):
        """写删除记录。归属 = finder_id(视频号 / 抖音 sec_uid),account_id 只记执行删除的卡片。

        platform 必须显式传：delete_logs 表 rev56 加了 platform 列，
        但写入口一直默认 'wx'，抖音的删除记录会被错标成视频号。
        """
        c = self._conn()
        try:
            c.execute("""INSERT INTO delete_logs
                (finder_id,account_id,comment_id,export_id,nickname,content,keyword,deleted_at,platform)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                      (finder_id, account_id, comment_id, export_id, nickname, content, keyword,
                       datetime.now().isoformat(), platform or "wx"))'''),
])

# ---------------- server ----------------
HELPER = '''def _comment_platform(comment_id, finder_id=None):
    """这条评论属于哪个平台。**库里那一行最权威**（一条评论只属于一个作品、一个平台）。

    查不到这一行（比如前端拿的是过期 id）才用 finder_id 反查；
    再查不到按视频号处理 —— 保持改动前的老行为，不因为加了抖音就把老路径改坏。
    """
    row = storage.get_comment(comment_id) or {}
    p = row.get("platform")
    if p:
        return platforms.normalize_platform(p), row
    if finder_id:
        p = storage.platform_of(finder_id)
        if p:
            return platforms.normalize_platform(p), row
    return platforms.PLATFORM_WX, row


def _dy_ready():
    """抖音写通道就绪判定。没挂页面时必须明确报错，绝不能静默走视频号通道。"""
    st = dy_manager.status()
    if not st.get("configured"):
        raise HTTPException(400, "抖音账号未配置（config 里没有 platform=dy 的账号）")
    if not st.get("page_ready"):
        raise HTTPException(400, "抖音页面未就绪：需要先挂载已登录的浏览器（/api/dy/attach）")
    return st


def _comment_worker(finder_id=None, account_id=None):'''

patch(SRV, [
    ("helper", "def _comment_worker(finder_id=None, account_id=None):", HELPER),
    ("platforms-import", "from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）",
     "from . import platforms                          # rev56: 平台维度抽象（wx / dy）\n"
     "from .douyin.manager import DouyinManager   # rev56: 抖音（platform=dy）"),

    # ---- 单条回复 ----
    ("reply-head", '''async def reply_comment(comment_id: str, body: schemas.ManualReply):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)''',
     '''async def reply_comment(comment_id: str, body: schemas.ManualReply):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        st = _dy_ready()
        aweme_id = str((crow or {}).get("export_id") or "")
        env = await dy_manager.reply(comment_id, aweme_id, body.content)
        if not env.get("__ok"):
            raise HTTPException(502, "回复失败: %s" % (env.get("__msg") or env.get("__err")))
        await hub.emit("comment_replied", {"comment_id": comment_id,
                                          "finder_id": (crow or {}).get("finder_id") or fid,
                                          "account_id": st.get("account_id") or "",
                                          "platform": "dy"})
        return {"ok": True, "platform": "dy"}
    w = _comment_worker(fid, body.account_id)'''),

    # ---- 单条删除 ----
    ("delete-head", '''async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)''',
     '''async def delete_comment(comment_id: str, body: schemas.DeleteCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        st = _dy_ready()
        aweme_id = str(body.export_id or (crow or {}).get("export_id") or "")
        env = await dy_manager.delete_comment(comment_id, aweme_id)
        if not env.get("__ok"):
            raise HTTPException(502, "删除失败: %s" % (env.get("__msg") or env.get("__err")))
        cinfo = crow or {}
        _fid = cinfo.get("finder_id") or fid
        storage.delete_comment(comment_id)
        storage.log_delete(_fid, comment_id, cinfo.get("nickname"), cinfo.get("content"),
                           "手动删除", aweme_id, st.get("account_id") or "", platform="dy")
        await hub.emit("comment_deleted", {"comment_id": comment_id, "platform": "dy"})
        return {"ok": True, "platform": "dy"}
    w = _comment_worker(fid, body.account_id)'''),

    # ---- 批量删除 ----
    ("batch", '''    for item in body.items:
        # 归属按视频号找登录卡片:评论行上的 account_id 可能已是被删掉的旧卡片
        fid = (getattr(item, "finder_id", None) or "").strip() or _fid_of(item.account_id)
        w = _comment_worker(fid, item.account_id)''',
     '''    for item in body.items:
        # 归属按视频号找登录卡片:评论行上的 account_id 可能已是被删掉的旧卡片
        fid = (getattr(item, "finder_id", None) or "").strip() or _fid_of(item.account_id)
        # 抖音评论走抖音通道（同一条批量删除里可能混着两个平台的评论）
        plat, crow = _comment_platform(item.comment_id, fid)
        if plat == platforms.PLATFORM_DY:
            try:
                st = _dy_ready()
                aweme_id = str(item.export_id or (crow or {}).get("export_id") or "")
                env = await dy_manager.delete_comment(item.comment_id, aweme_id)
                if not env.get("__ok"):
                    failed.append({"comment_id": item.comment_id,
                                   "error": "删除失败: %s" % (env.get("__msg") or env.get("__err"))})
                    continue
                cinfo = crow or {}
                storage.delete_comment(item.comment_id)
                storage.log_delete(cinfo.get("finder_id") or fid, item.comment_id,
                                   cinfo.get("nickname"), cinfo.get("content"),
                                   "手动删除", aweme_id, st.get("account_id") or "",
                                   platform="dy")
                await hub.emit("comment_deleted", {"comment_id": item.comment_id,
                                                   "platform": "dy"})
                deleted += 1
            except HTTPException as e:
                failed.append({"comment_id": item.comment_id, "error": str(e.detail)})
            except Exception as e:
                failed.append({"comment_id": item.comment_id, "error": str(e)})
            continue
        w = _comment_worker(fid, item.account_id)'''),

    # ---- 置顶 ----
    ("pin", '''async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    w = _comment_worker(fid, body.account_id)''',
     '''async def pin_comment(comment_id: str, body: schemas.PinCommentBody):
    fid = (body.finder_id or "").strip() or _fid_of(body.account_id)
    plat, _crow = _comment_platform(comment_id, fid)
    if plat == platforms.PLATFORM_DY:
        # 抖音评论置顶接口还没探明（作品置顶是另一条 item/top）。宁可明确报错，
        # 也不能让它悄悄走视频号通道然后报「没有已登录的卡片」误导排查。
        raise HTTPException(400, "抖音评论置顶接口尚未探明，暂不支持")
    w = _comment_worker(fid, body.account_id)'''),
])


# ---------------- 自校验 ----------------
def selfcheck():
    a = io.open(STO, encoding="utf-8").read()
    b = io.open(SRV, encoding="utf-8").read()
    checks = [
        ("storage.get_comment 返回 platform", 'content,platform "' in a or "content,platform " in a),
        ("storage.get_comment dict 带 platform", 'platform=(r[5] or "wx")' in a),
        ("log_delete 支持 platform 参数", 'account_id=None, platform="wx"' in a),
        ("log_delete INSERT 带 platform", "keyword,deleted_at,platform)" in a),
        ("server 有 _comment_platform", "def _comment_platform(" in b),
        ("server 有 _dy_ready", "def _dy_ready():" in b),
        ("platforms 已导入", "from . import platforms" in b),
        ("回复已分流", 'plat == platforms.PLATFORM_DY:\n        st = _dy_ready()\n        aweme_id' in b),
        ("删除已分流", 'if plat == platforms.PLATFORM_DY:\n        st = _dy_ready()\n        aweme_id = str(body.export_id' in b),
        ("批量删除已分流", "同一条批量删除里可能混着两个平台的评论" in b),
        ("置顶明确拒绝", "抖音评论置顶接口尚未探明，暂不支持" in b),
        ("批量删除：抖音分支在视频号 worker 之前",
         b.find("if plat == platforms.PLATFORM_DY:",
                b.find("async def batch_delete_comments"))
         < b.find("w = _comment_worker(fid, item.account_id)",
                  b.find("async def batch_delete_comments"))),
    ]
    bad = 0
    for n, ok in checks:
        print(("  PASS  " if ok else "  FAIL  ") + n)
        bad += 0 if ok else 1
    return bad


print("=== 自校验 ===")
bad = selfcheck()

print("=== 语法检查 ===")
import py_compile  # noqa: E402
for p in (STO, SRV):
    try:
        py_compile.compile(p, doraise=True)
        print("  OK   " + os.path.basename(p))
    except Exception as e:
        bad += 1
        print("  FAIL " + os.path.basename(p) + " : " + str(e)[:200])

print("\n结果:", "ALL PASS" if not bad else "有 %d 项失败" % bad)
raise SystemExit(0 if not bad else 1)
