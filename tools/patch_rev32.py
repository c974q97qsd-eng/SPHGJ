# -*- coding: utf-8 -*-
"""rev32 一次性补丁: profile_dir 指向扫码临时目录的自愈。

背景(2026-10-21 复查 config):
  uspoloassn8  -> ./profiles/_login_24d3ba562ba6   (目录还在,能救)
  uspoloassn9  -> ./profiles/_login_63d7f401d83f   (目录已消失,登录态已丢)
  uspoloassn10 -> ./profiles/_login_846d23d95097   (目录已消失,登录态已丢)

根因: relogin 时 `_move_profile_to(target)` 在目标 profile 被占用(原 worker 没停干净)
时会 rmtree 失败 -> shutil.move 失败 -> 降级返回临时目录,并且**把这个临时路径
写进了 config**。而临时目录 `_login_*` 会被登录收尾逻辑清掉 —— 一旦清掉,账号
就永久登录不上(9/10 号现状)。

改动:
1. AccountManager.heal_login_profiles(): 启动时(账号还没起来、profile 空着)
   - 临时目录还在 -> 搬到 ./profiles/<id>,更新 config
   - 临时目录已消失 -> 重置为 ./profiles/<id>(建空目录),清掉 storage_state,
     日志明确提示需要重新扫码
2. LoginCapture._move_profile_to(): 删目标失败时先清孤儿再重试,避免再次降级
3. server startup: 在自动恢复账号之前调用自愈
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATCHES = {
 "backend/account_manager.py": [
  (
'''    async def restore_running_accounts(self, gap_sec: float = 4.0):''',
'''    async def heal_login_profiles(self):
        """启动自愈:profile_dir 还指向扫码临时目录(_login_*)的账号搬回正常目录(rev32)。

        为什么要做:relogin 的 `_move_profile_to` 在目标被占用时会降级返回临时目录,
        并把临时路径写进 config。而 `_login_*` 目录是会被清掉的 —— 清掉之后账号就
        永久登录不上(线上 9/10 号就是这么废的)。

        必须在「账号被自动恢复拉起之前」执行:那时 profile 还空着,才能安全搬目录。
        返回 (搬迁数, 重置数)。
        """
        import shutil
        healed = repaired = 0
        for acc in self.config.get("accounts", []):
            pd = (acc.get("profile_dir") or "").replace("\\\\", "/")
            if "_login_" not in pd:
                continue
            aid = acc["id"]
            dst = f"./profiles/{aid}"
            if not os.path.isdir(pd):
                # 临时目录已经被清理 -> 登录态彻底没了,重置到正常路径,等用户重新扫码
                logger.warning(f"[自愈] {aid} 的 profile_dir 指向已消失的临时目录 {pd},"
                               f"登录态已丢失,重置为 {dst}(需要重新扫码)")
                try:
                    os.makedirs(dst, exist_ok=True)
                except Exception as e:
                    logger.warning(f"[自愈] {aid} 创建目录失败: {e}")
                    continue
                acc["profile_dir"] = dst
                acc.pop("storage_state", None)
                repaired += 1
                continue
            if os.path.isdir(dst):
                # 目标已存在(旧的失效 profile):清掉占用再替换,临时目录里才是最新登录态
                try:
                    reap_profile_orphans(dst, log_tag=f"[自愈:{aid}]", min_age_sec=5)
                except Exception:
                    pass
                shutil.rmtree(dst, ignore_errors=True)
                if os.path.isdir(dst):
                    logger.warning(f"[自愈] {aid} 目标 profile 被占用,本次跳过({dst})")
                    continue
            try:
                os.rename(pd, dst)
            except Exception as e:
                logger.warning(f"[自愈] {aid} 搬迁失败({pd} -> {dst}): {e}")
                continue
            acc["profile_dir"] = dst
            healed += 1
            logger.info(f"[自愈] {aid} profile 已从 {pd} 搬到 {dst}")
        if healed or repaired:
            self._save_config()
            logger.info(f"[自愈] 完成: 搬迁 {healed} 个, 重置 {repaired} 个(需重新扫码)")
        else:
            logger.info("[自愈] 没有指向临时目录的账号 profile,无需处理")
        return healed, repaired

    async def restore_running_accounts(self, gap_sec: float = 4.0):''',
   "新增 heal_login_profiles"),
 ],

 "backend/login_capture.py": [
  (
'''        try:
            import shutil
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(self.profile_dir, target_dir)''',
'''        try:
            import shutil
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
                if os.path.isdir(target_dir):
                    # rev32: 目标被残留 Chromium 占着删不掉 —— 以前直接降级成临时目录,
                    # 而这个临时路径会被写进 config,临时目录一清账号就永久登录不上
                    # (9/10 号的事故)。这里先清孤儿再删一次。
                    reap_profile_orphans(target_dir,
                                         log_tag=f"[login:{self.sid}] 迁移前", min_age_sec=0)
                    time.sleep(1.0)
                    shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(self.profile_dir, target_dir)''',
   "_move_profile_to 加固"),
 ],

 "backend/server.py": [
  (
'''    async def _auto_restore_loop():
        await asyncio.sleep(15)''',
'''    async def _auto_restore_loop():
        # rev32: 先把指向扫码临时目录的 profile 搬回正常路径。
        # 必须赶在自动恢复拉起账号之前(profile 空着才搬得动)。
        try:
            await manager.heal_login_profiles()
        except Exception as e:
            logger.warning(f"[自愈] 执行异常(忽略): {e}")
        await asyncio.sleep(15)''',
   "startup 调用自愈"),
 ],
}


def main():
    for rel, items in PATCHES.items():
        path = os.path.join(ROOT, rel)
        src = io.open(path, encoding="utf-8").read()
        print("== %s ==" % rel)
        for old, new, tag in items:
            n = src.count(old)
            if n != 1:
                raise SystemExit("FAIL 命中 %d 次(需恰好 1): %s -> %s" % (n, tag, rel))
            src = src.replace(old, new, 1)
            print("  OK  %s" % tag)
        io.open(path, "w", encoding="utf-8", newline="").write(src)

    print("\n== 自校验 ==")
    checks = [
        ("backend/account_manager.py", "async def heal_login_profiles"),
        ("backend/account_manager.py", "acc.pop(\"storage_state\", None)"),
        ("backend/login_capture.py", "迁移前"),
        ("backend/server.py", "await manager.heal_login_profiles()"),
    ]
    bad = 0
    for rel, needle in checks:
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        ok = needle in s
        print("  %s  %-40s %s" % ("PASS" if ok else "FAIL", needle[:40], rel))
        if not ok:
            bad += 1
    print("\nSELFCHECK", "PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


sys.exit(main())
