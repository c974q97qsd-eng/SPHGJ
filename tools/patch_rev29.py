# -*- coding: utf-8 -*-
"""rev29 一次性补丁。

修复两个现象（2026-10-20 免税仓 uspoloassn2）：
1. 扫码 5 次都成功（_aid/finder_id 全抓到、config 也写对了），但账号永远登录不上。
   根因不是"扫不上"，是**登录态没落盘**：
   - Cookies 文件 mtime 一直停在清空那一刻，weixin 域 0 条；
   - 扫码成功后代码立刻 `_close(keep_profile=True)`，Chromium 的 cookie 是延迟
     批量写盘的，窗口只活了 10 秒就关，写盘队列被丢弃 → 关掉就回到登录页；
   - 而且 `ctx.close()` 抛异常时紧跟 `reap_profile_orphans()` 无条件 kill，
     进程被砍在半路，更加不可能 flush；
   - rev28 新加的"扫码成功自动 start_account"与登录会话收尾并发，
     reap 把刚 launch 起来的账号浏览器当孤儿杀掉（Target page/browser has been closed）。
2. 二维码图片带大片黑边：选择器未命中时 fallback 直接推**整页截图**（273KB），
   把登录页下方「用微信扫描上方二维码…」和深色背景一起推给前端。

改动：
- browser.reap_profile_orphans 增加 min_age_sec，刚启动的进程豁免；
- close_context_safely 兜底 kill 传 min_age_sec=4；
- login_capture: captured 后主动导出 storage_state + 等 2.5s 再关（给 flush 时间）；
- login_capture: _close 收尾 reap 加 min_age_sec=10；
- login_capture: finalize 把 storage_state 写进 config 账号；
- login_capture: 整页 fallback 改为【只裁二维码方块】，裁不到就不推图；
- account_manager: _open_context 启动后注入 storage_state cookies（双保险，
  即使 profile 磁盘 cookie 永远写不进也能登录）；
- account_manager: _on_relogin_finished 延迟 1.5s 再 start，避开收尾清理；
- account_manager: 两处启动前/扫码前 reap 传 min_age_sec=5。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATCHES = {
 "backend/browser.py": [
  # 0) 补 import time
  (
'''import os
import logging''',
'''import os
import time
import logging''',
   "补 import time"),
  # 1) reap 增加最小存活时间保护
  (
'''def reap_profile_orphans(profile_dir, log_tag=""):''',
'''def reap_profile_orphans(profile_dir, log_tag="", min_age_sec=0):''',
   "reap 签名加 min_age_sec"),
  (
'''    if killed:
        more = "…" if len(killed) > 8 else ""
        logger.info(f"[browser] {log_tag} 清理 {len(killed)} 个孤儿 chrome 进程 "
                    f"pids={killed[:8]}{more} profile={profile_dir}")
    return killed''',
'''    if killed:
        more = "…" if len(killed) > 8 else ""
        logger.info(f"[browser] {log_tag} 清理 {len(killed)} 个孤儿 chrome 进程 "
                    f"pids={killed[:8]}{more} profile={profile_dir}")
    return killed''',
   "占位（下面单独 patch 函数体）"),
  (
'''    abs_dir = os.path.abspath(profile_dir)
    killed = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            for arg in (p.info.get("cmdline") or []):
                if arg.startswith("--user-data-dir="):
                    if os.path.abspath(arg.split("=", 1)[1]) == abs_dir:
                        pid = p.info["pid"]
                        p.kill()
                        killed.append(pid)
                        break''',
'''    abs_dir = os.path.abspath(profile_dir)
    killed = []
    # rev29: min_age_sec 保护 —— 刚 launch 出来正在登录/扫码的进程绝不能被当孤儿杀掉。
    # 免税仓事故：扫码成功后自动 start_account 拉起浏览器，紧接着登录会话收尾的
    # reap 把它杀了 -> "Target page, context or browser has been closed"。
    now = time.time()
    for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            age = now - (p.info.get("create_time") or 0)
            if min_age_sec > 0 and age < min_age_sec:
                continue
            for arg in (p.info.get("cmdline") or []):
                if arg.startswith("--user-data-dir="):
                    if os.path.abspath(arg.split("=", 1)[1]) == abs_dir:
                        pid = p.info["pid"]
                        p.kill()
                        killed.append(pid)
                        break''',
   "reap 加 min_age 过滤"),
  # 2) 兜底 kill 加保护
  (
'''    reap_profile_orphans(profile_dir, log_tag=f"{log_tag} 兜底")''',
'''    reap_profile_orphans(profile_dir, log_tag=f"{log_tag} 兜底", min_age_sec=4)''',
   "close 兜底加 min_age"),
 ],

 "backend/login_capture.py": [
  # 3) __init__ 增加 _storage_state
  (
'''        self._finder_list_cache = []''',
'''        self._finder_list_cache = []
        # rev29: 扫码成功后导出的登录态(cookies),作为 profile 磁盘落盘失败的双保险
        self._storage_state = None''',
   "初始化 _storage_state"),
  # 4) captured 段：导出 storage_state + 等落盘再关
  (
'''            self.status = "captured"
            logger.info(f"[login:{self.sid}] 抓取完成 aid/finder_id 齐全 name={self.captured['name']}")
            await self.emit("login_status", {"sid": self.sid, "status": "captured",
                                             "captured": dict(self.captured)})''',
'''            self.status = "captured"
            logger.info(f"[login:{self.sid}] 抓取完成 aid/finder_id 齐全 name={self.captured['name']}")
            # rev29: 关浏览器前先把登录态导出来存住。
            # Chromium 的 cookie 是延迟批量写盘的(不是每次变更立刻落盘),扫码成功后
            # 立刻 close 会把写盘队列丢掉 -> profile 里的 Cookies 一条都不多,下次启动
            # 又是登录页。这就是免税仓「扫码 5 次全成功却始终登录不上」的真因。
            # 导出 storage_state 后:①写进 config 账号,启动时 add_cookies 注入(双保险);
            # ②再等 2.5s 给 Chromium 自己 flush 到 profile 的时间,两条路总有一条生效。
            try:
                if self.context:
                    self._storage_state = await self.context.storage_state()
                    n = len((self._storage_state or {}).get("cookies") or [])
                    logger.info(f"[login:{self.sid}] 已导出登录态 cookies={n} 条")
            except Exception as e:
                logger.warning(f"[login:{self.sid}] 导出登录态失败(仅影响双保险): {e}")
            await self.emit("login_status", {"sid": self.sid, "status": "captured",
                                             "captured": dict(self.captured)})
            await asyncio.sleep(2.5)''',
   "captured 导出 storage_state + 等落盘"),
  # 5) _close 收尾 reap 加保护
  (
'''                reap_profile_orphans(self.profile_dir, log_tag=f"[login:{self.sid}] 收尾")''',
'''                reap_profile_orphans(self.profile_dir, log_tag=f"[login:{self.sid}] 收尾",
                                     min_age_sec=10)''',
   "_close 收尾 reap 加 min_age"),
  # 6) finalize 写 storage_state 进 config
  (
'''            acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            self._save_config()''',
'''            acc["login_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            if self._storage_state and (self._storage_state.get("cookies") or []):
                # rev29: 登录态快照随 config 保存,账号启动时注入(防 profile 落盘失败)
                acc["storage_state"] = self._storage_state
            self._save_config()''',
   "finalize 写 storage_state"),
  # 7) 整页 fallback 改为只裁二维码
  (
'''        # 4. fallback:整页截图
        try:
            png = await self.page.screenshot(timeout=5000)
            if len(png) > 1000:
                logger.info(f"[login:{self.sid}] 二维码选择器未命中,fallback 整页截图 bytes={len(png)}")
                return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception as e:
            logger.warning(f"[login:{self.sid}] 整页截图失败: {e}")
        return None''',
'''        # 4. fallback:整页截图 -> 只裁二维码区域(rev29)
        #    原来直接把整页推给前端:登录页下方「用微信扫描上方二维码…」+ 深色背景
        #    一起进了弹窗(又大又难看);账号选择页 fallback 更是完全不相干的图。
        #    现在先在页面里找方形二维码元素,裁得到才推,裁不到就不推本轮。
        try:
            box = await self.page.evaluate("""() => {
                const cands = [];
                const els = document.querySelectorAll('img, canvas, .qrcode, [class*=qrcode], [class*=qr-code]');
                els.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width >= 120 && r.height >= 120 && Math.abs(r.width - r.height) <= 40) {
                        cands.push({x: r.x, y: r.y, width: r.width, height: r.height, area: r.width * r.height});
                    }
                });
                if (!cands.length) return null;
                cands.sort((a, b) => b.area - a.area);
                const c = cands[0];
                return {x: Math.round(c.x), y: Math.round(c.y), width: Math.round(c.width), height: Math.round(c.height)};
            }""")
            if box and box.get("width", 0) >= 120:
                png = await self.page.screenshot(clip=box, timeout=5000)
                if len(png) > 1000:
                    logger.info(f"[login:{self.sid}] 整页 fallback 裁剪出二维码 {box} bytes={len(png)}")
                    return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception as e:
            logger.debug(f"[login:{self.sid}] fallback 裁剪二维码失败: {e}")
        logger.info(f"[login:{self.sid}] 本轮未定位到二维码,不推送图片")
        return None''',
   "fallback 只裁二维码"),
 ],

 "backend/account_manager.py": [
  # 8) _open_context 注入 storage_state
  (
'''        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()''',
'''        self.context = await launch_stealth(self._pw, self.account["profile_dir"], headless=self._headless)
        # rev29: 注入扫码时导出的登录态(双保险)。profile 磁盘 cookie 有时写入不成功
        # (Chromium 延迟写盘 + 进程被中途关闭),只靠磁盘会反复「扫码成功但登录不上」。
        try:
            _ss = (self.account.get("storage_state") or {})
            _ck = _ss.get("cookies") or []
            if _ck:
                await self.context.add_cookies(_ck)
                logger.info(f"[{self.account['id']}] 已注入登录态 cookies={len(_ck)} 条")
        except Exception as e:
            logger.warning(f"[{self.account['id']}] 注入登录态失败(回退用 profile 磁盘): {e}")
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()''',
   "账号启动注入 cookies"),
  # 9) start_account 前 reap 加保护
  (
'''        if not self._profile_busy(account["id"]):
            try:
                reap_profile_orphans(account.get("profile_dir"),
                                     log_tag=f"[{account['id']}] 启动前")''',
'''        if not self._profile_busy(account["id"]):
            try:
                reap_profile_orphans(account.get("profile_dir"),
                                     log_tag=f"[{account['id']}] 启动前",
                                     min_age_sec=5)''',
   "add_account 前 reap 加保护"),
  (
'''            reap_profile_orphans(acc.get("profile_dir"), log_tag=f"[{account_id}] 扫码前")''',
'''            reap_profile_orphans(acc.get("profile_dir"), log_tag=f"[{account_id}] 扫码前",
                                 min_age_sec=5)''',
   "relogin 前 reap 加保护"),
  # 10) 延迟启动，避开登录会话收尾
  (
'''        if status == "finalized":
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"relogin 后启动失败 {account_id}: {e}")''',
'''        if status == "finalized":
            # rev29: 等登录会话把 profile 彻底释放再启动。以前立刻 start,和会话收尾的
            # 孤儿清理并发,刚 launch 的浏览器被 reap 当孤儿杀掉(Target closed)。
            await asyncio.sleep(1.5)
            try:
                await self.start_account(account_id)
            except Exception as e:
                logger.warning(f"relogin 后启动失败 {account_id}: {e}")''',
   "延迟后启动"),
 ],
}


def main():
    for rel, items in PATCHES.items():
        path = os.path.join(ROOT, rel)
        src = io.open(path, encoding="utf-8").read()
        orig = src
        applied = 0
        for old, new, tag in items:
            if old == new:
                print("  SKIP(占位) %-28s %s" % (tag, rel))
                continue
            n = src.count(old)
            if n != 1:
                raise SystemExit("FAIL 命中 %d 次(需恰好 1): %s -> %s" % (n, tag, rel))
            src = src.replace(old, new, 1)
            applied += 1
            print("  OK  %-30s %s" % (tag, rel))
        if applied:
            io.open(path, "w", encoding="utf-8", newline="").write(src)
        print("%s: 改动 %d 处, %d -> %d 字节" % (rel, applied, len(orig), len(src)))

    # 自校验
    print("\n== 自校验 ==")
    checks = [
        ("backend/browser.py", 'def reap_profile_orphans(profile_dir, log_tag="", min_age_sec=0):'),
        ("backend/browser.py", 'min_age_sec=4)'),
        ("backend/browser.py", 'now = time.time()'),
        ("backend/login_capture.py", 'self._storage_state = None'),
        ("backend/login_capture.py", 'await asyncio.sleep(2.5)'),
        ("backend/login_capture.py", 'acc["storage_state"] = self._storage_state'),
        ("backend/login_capture.py", '整页 fallback 裁剪出二维码'),
        ("backend/login_capture.py", 'min_age_sec=10)'),
        ("backend/account_manager.py", '已注入登录态 cookies='),
        ("backend/account_manager.py", 'await asyncio.sleep(1.5)'),
    ]
    bad = 0
    for rel, needle in checks:
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        ok = needle in s
        print("  %s  %-46s %s" % ("PASS" if ok else "FAIL", needle[:46], rel))
        if not ok:
            bad += 1
    print("\nSELFCHECK", "PASS" if bad == 0 else "FAIL(%d)" % bad)
    return 0 if bad == 0 else 1


sys.exit(main())
