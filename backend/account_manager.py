"""多账号管理 + 扫码登录会话。

- AccountWorker:每账号独立 Playwright 持久化 profile + 评论页 + 并行轮询
- AccountManager:管理 workers + 登录会话 + 自动评论热更新 + 状态上报
- 登录态检测:未登录跳登录页;LoginSession 负责扫码 + 字段抓取(见 login_capture.py)
"""
import os
import json
import asyncio
from datetime import datetime
import logging
logger = logging.getLogger("sphgj")
from playwright.async_api import async_playwright
from .api_client import WxApiClient
from .comment_fetcher import CommentFetcher
from .auto_reply import AutoReply
from .auto_delete import AutoDelete
from .live_fetcher import LiveFetcher
from .auto_comment import AutoCommenter
from .login_capture import LoginSession
from .browser import launch_stealth
from .selectors import COMMENT_URL


class AccountWorker:
    def __init__(self, account, storage, config, emit=None):
        self.account = account
        self.storage = storage
        self.config = config
        self.emit = emit  # async emit(event, payload)
        self.api = None
        self.fetcher = None
        self.auto_reply = None
        self.auto_commenter = None
        self.auto_delete = None
        self.live_page = None
        self.live_fetcher = None
        self._live_loop_task = None
        self.live_info = None
        self.page = None
        self.context = None
        self._loop_task = None
        self._running = False
        self.logged_in = False
        self.last_scan_at = None
        self.new_count = 0

    async def start(self, playwright, headless=True):
        os.makedirs(self.account["profile_dir"], exist_ok=True)
        self.context = await launch_stealth(playwright, self.account["profile_dir"], headless=headless)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto(COMMENT_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)
        if "interaction/comment" in (self.page.url or ""):
            self.logged_in = True
            logger.info(f"[{self.account['id']}] 已登录")
        else:
            self.logged_in = False
            logger.warning(f"[{self.account['id']}] 未登录(当前:{self.page.url}),请重新扫码")
        self.api = WxApiClient(self.page, self.account)
        self.auto_reply = AutoReply(self.api, self.storage, self.account["id"],
                                    self.config.get("auto_reply") or {})
        self.auto_commenter = AutoCommenter(self.api, self.storage, self.account)
        self.auto_delete = AutoDelete(self.api, self.storage, self.account["id"],
                                      self.config.get("auto_delete") or {})
        self.fetcher = CommentFetcher(self.api, self.storage, self.account["id"],
                                      self.auto_reply, self.auto_commenter, self.auto_delete)
        # 直播大屏:登录后开 liveBuild page
        if self.logged_in:
            try:
                self.live_page = await self.context.new_page()
                self.live_fetcher = LiveFetcher(self.live_page, self.account["id"])
                await self.live_fetcher.goto_live()
            except Exception as e:
                logger.warning(f"[{self.account['id']}] 直播页启动失败: {e}")
        self._running = True

    def start_loop(self):
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._fetch_loop())

    async def _fetch_loop(self):
        interval = self.config.get("fetch_interval_sec", 300)
        while self._running:
            if self.logged_in and self.fetcher:
                try:
                    scanned, new_c, new_comments, deleted_ids = await self.fetcher.fetch_all()
                    self.last_scan_at = datetime.now().isoformat()
                    if new_c:
                        self.new_count += new_c
                    logger.info(f"[{self.account['id']}] 扫描{scanned}视频 新增{new_c}评论")
                    if new_comments:
                        await self._emit("comments_update",
                                         {"account_id": self.account["id"], "comments": new_comments})
                    for cid in deleted_ids:
                        await self._emit("comment_deleted", {"comment_id": cid})
                except Exception as e:
                    logger.error(f"[{self.account['id']}] 抓取异常: {e}")
            await asyncio.sleep(interval)

    async def fetch_once(self):
        if self.logged_in and self.fetcher:
            return await self.fetcher.fetch_all()
        return 0, 0, [], []

    def start_live_loop(self):
        if self._live_loop_task is None or self._live_loop_task.done():
            self._live_loop_task = asyncio.create_task(self._live_loop())

    async def _live_loop(self):
        while self._running:
            if self.logged_in and self.live_fetcher:
                try:
                    info = await self.live_fetcher.fetch()
                    self.live_info = info
                    await self._emit("live_screen_update", {
                        "account_id": self.account["id"],
                        "name": self.account.get("name", self.account["id"]),
                        **info,
                    })
                except Exception as e:
                    logger.error(f"[{self.account['id']}] 直播抓取异常: {e}")
            await asyncio.sleep(4)

    async def _emit(self, event, payload):
        if self.emit:
            try:
                await self.emit(event, payload)
            except Exception as e:
                logger.debug(f"emit 失败 {event}: {e}")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except Exception:
                pass
        if self._live_loop_task:
            self._live_loop_task.cancel()
            try:
                await self._live_loop_task
            except Exception:
                pass
        if self.context:
            await self.context.close()


class AccountManager:
    def __init__(self, config, storage, emit=None):
        self.config = config
        self.storage = storage
        self.emit = emit  # async emit(event, payload)
        self.workers = {}          # account_id -> AccountWorker
        self.login_sessions = {}   # sid -> LoginSession
        self._playwright = None
        self._running = False

    async def _ensure_playwright(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _emit(self, event, payload):
        if self.emit:
            try:
                await self.emit(event, payload)
            except Exception as e:
                logger.debug(f"emit 失败 {event}: {e}")

    # ---------- 扫码登录 ----------
    async def start_login(self, headed=False):
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit)
        self.login_sessions[sess.sid] = sess
        await sess.start(headed=headed)
        return sess

    def get_login_session(self, sid):
        return self.login_sessions.get(sid)

    async def open_window_login(self, sid):
        s = self.login_sessions.get(sid)
        if s:
            await s.open_window()
        return s

    async def cancel_login(self, sid):
        s = self.login_sessions.pop(sid, None)
        if s:
            await s.cancel()

    async def finalize_login(self, sid, account_id=None, name=None):
        s = self.login_sessions.pop(sid, None)
        if not s:
            return None
        return await s.finalize_with_id(account_id, name)

    # ---------- 引擎 ----------
    async def start(self, headless=True):
        await self._ensure_playwright()
        self._running = True
        for acc in self.config.get("accounts", []):
            try:
                await self.add_account(acc, headless=headless)
            except Exception as e:
                logger.error(f"账号 {acc.get('id')} 启动失败: {e}")
        for w in self.workers.values():
            w.start_loop()
            w.start_live_loop()
        await self._emit("engine_status", self.status_snapshot())

    async def stop(self):
        self._running = False
        for w in list(self.workers.values()):
            await w.stop()
        self.workers.clear()
        await self._emit("engine_status", self.status_snapshot())

    async def add_account(self, account, headless=True):
        worker = AccountWorker(account, self.storage, self.config, self._emit)
        await worker.start(self._playwright, headless=headless)
        self.workers[account["id"]] = worker
        return worker

    async def start_account(self, account_id):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        await self._ensure_playwright()
        if account_id in self.workers:
            return self.workers[account_id]
        w = AccountWorker(acc, self.storage, self.config, self._emit)
        await w.start(self._playwright, headless=True)
        if w.logged_in:
            w.start_loop()
            w.start_live_loop()
        else:
            # 未登录:不进入运行态,等前端触发 relogin 扫码
            w._running = False
        self.workers[account_id] = w
        await self._emit("engine_status", self.status_snapshot())
        return w

    async def start_relogin(self, account_id):
        """已存在账号重新扫码登录:复用原 profile_dir(headed),成功后更新原账号字段。"""
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return None
        if account_id in self.workers:
            await self.stop_account(account_id)
        pw = await self._ensure_playwright()
        sess = LoginSession(pw, self.config, self._emit, account=acc)
        self.login_sessions[sess.sid] = sess
        await sess.start(headed=True)
        return sess

    async def stop_account(self, account_id):
        w = self.workers.pop(account_id, None)
        if w:
            await w.stop()
        await self._emit("engine_status", self.status_snapshot())

    async def fetch_all_once(self):
        tasks = [w.fetch_once() for w in self.workers.values() if w.logged_in]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def get_worker(self, account_id):
        return self.workers.get(account_id)

    # ---------- 自动评论热更新(诉求2:账号管理模块内可随时改) ----------
    def update_auto_comment(self, account_id, enabled, content):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        acc["auto_comment_enabled"] = bool(enabled)
        acc["auto_comment_content"] = content or ""
        self._save_config()
        w = self.workers.get(account_id)
        if w and w.auto_commenter:
            w.auto_commenter.account["auto_comment_enabled"] = bool(enabled)
            w.auto_commenter.account["auto_comment_content"] = content or ""
        return True

    def update_account(self, account_id, name=None):
        acc = next((a for a in self.config.get("accounts", []) if a["id"] == account_id), None)
        if not acc:
            return False
        if name is not None:
            acc["name"] = name
        self._save_config()
        return True

    def delete_account(self, account_id, remove_profile=False):
        before = len(self.config.get("accounts", []))
        self.config["accounts"] = [a for a in self.config.get("accounts", []) if a["id"] != account_id]
        if len(self.config["accounts"]) == before:
            return False
        self._save_config()
        if remove_profile:
            import shutil
            shutil.rmtree(f"./profiles/{account_id}", ignore_errors=True)
        return True

    def _save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    # ---------- 状态 ----------
    def status_snapshot(self):
        stats = {s["account_id"]: s for s in self.storage.account_stats()}
        return {
            "running": self._running,
            "accounts": [self._account_status(a, stats) for a in self.config.get("accounts", [])],
        }

    def _account_status(self, acc, stats):
        w = self.workers.get(acc["id"])
        st = stats.get(acc["id"], {})
        return {
            "id": acc["id"],
            "name": acc.get("name", acc["id"]),
            "logged_in": bool(w and w.logged_in),
            "running": bool(w and w._running),
            "last_scan": getattr(w, "last_scan_at", None) if w else None,
            "new_comments": getattr(w, "new_count", 0) if w else 0,
            "total_comments": st.get("total", 0),
            "replied": st.get("replied", 0),
            "last_fetched": st.get("last_fetched"),
            "auto_comment_enabled": bool(acc.get("auto_comment_enabled")),
            "auto_comment_content": acc.get("auto_comment_content", ""),
            "has_aid": bool(acc.get("_aid")),
        }

    async def stop_all(self):
        """进程退出前清理:停 workers + 登录会话 + playwright。"""
        await self.stop()
        for s in list(self.login_sessions.values()):
            await s.cancel()
        self.login_sessions.clear()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
