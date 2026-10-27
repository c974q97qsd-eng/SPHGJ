"""抖音侧运行容器：把「一个抖音账号」需要的东西收在一处。

职责边界（刻意收窄，避免和视频号那套纠缠）：
  · 从 config 里找 platform=dy 的账号
  · 持有一个 page（由登录/浏览器生命周期注入），据此建 DouyinClient
  · 对外只暴露四件事：status / fetch_now / reply / delete_comment
  · 归属键（sec_uid）第一次抓到身份后回填到 config 并落盘

**不负责**登录与浏览器启停。抖音的登录流程（creator.douyin.com 扫码）和视频号
完全不同，硬塞进 AccountWorker 会把已经稳定运行的状态机搞乱。所以这里的
`page` 是一个注入点：谁启动浏览器谁调 `set_page(page)`；没注入时所有需要网络的
方法都返回「未就绪」，绝不假装成功。
"""
import logging
import time

from .. import platforms
from . import api
from .client import DouyinClient
from .fetcher import DouyinCommentFetcher

logger = logging.getLogger("sphgj")


class DouyinManager:
    def __init__(self, config, storage, emit=None, save_config=None):
        self.config = config or {}
        self.storage = storage
        self.emit = emit
        self._save_config = save_config
        self._page = None
        self._client = None
        self._fetcher = None
        self._last_report = None
        self._identity = {}
        self._last_error = ""

    # ---------------- 账号 / 归属 ----------------
    def accounts(self):
        return [a for a in (self.config.get("accounts") or [])
                if platforms.account_platform(a) == platforms.PLATFORM_DY]

    def account(self):
        accs = self.accounts()
        return accs[0] if accs else {}

    @property
    def account_id(self):
        return str(self.account().get("id") or "")

    def owner(self):
        """归属键。拿到 sec_uid 之前是 acct:<id>，抓到作品后自动升级成真值。"""
        return platforms.owner_key(self.account(), self.account_id)

    # ---------------- page / client ----------------
    def set_page(self, page):
        """注入/解绑页面。解绑时必须传 None —— 否则会拿着死 page 白发请求。"""
        self._page = page
        if page is None:
            self._client = None
            self._fetcher = None
            return
        # 每次绑定都新建 client，所以加密 item_id 缓存（_item_enc）随旧 client
        # 一起丢掉 —— 换账号/重连时不会拿着上一账号的加密串去打接口。
        self._client = DouyinClient(page, self.account(), config=self.config,
                                    storage=self.storage)
        self._fetcher = DouyinCommentFetcher(self._client, self.storage,
                                             self.account_id, self.owner(),
                                             config=self.config)
        logger.info("[%s] 抖音页面已绑定" % self.account_id)

    def _need_client(self):
        if not self.account_id:
            return None, "未配置抖音账号（config.accounts 里没有 platform=dy 的项）"
        if self._client is None:
            return None, "抖音页面未就绪（浏览器没启动或未登录）"
        return self._client, ""

    def status(self):
        acc = self.account()
        owner = self.owner()
        try:
            total = self.storage.query_comments(finder_id=owner, limit=1, offset=0)[1]
        except Exception:
            total = 0
        rep = self._last_report or {}
        return {
            "platform": "dy",
            "configured": bool(acc),
            "account_id": self.account_id,
            "name": acc.get("name") or "",
            "owner": owner,
            "owner_fallback": platforms.owner_is_fallback(owner),
            "uid": acc.get("_dy_uid") or "",
            "sec_uid": acc.get("_dy_sec_uid") or "",
            "page_ready": self._client is not None,
            "logged_in": (self._client is not None and self._client.auth_fail is None),
            "comments_total": total,
            "last_fetch": ({k: rep.get(k) for k in
                            ("works", "checked", "new", "skipped", "complete",
                             "seconds", "mode", "note")} if rep else None),
            "last_fetch_at": rep.get("at") if rep else None,
            "last_error": self._last_error,
        }

    # ---------------- 身份回填 ----------------
    async def refresh_identity(self):
        """用作品列表里的 author 拿 uid / sec_uid，回填 config 并落盘。

        归属键从 acct:<id> 升成真 sec_uid 就靠这一步；升完要重建 fetcher，
        否则它还拿着旧的 owner。
        """
        client, why = self._need_client()
        if client is None:
            return {}
        ident = await client.self_identity()
        if not ident:
            self._last_error = "取不到账号身份（作品列表为空或未登录）"
            return {}
        acc = self.account()
        changed = False
        for key, val in (("_dy_uid", ident.get("uid")), ("_dy_sec_uid", ident.get("sec_uid")),
                         ("_dy_unique_id", ident.get("unique_id"))):
            if val and str(acc.get(key) or "") != str(val):
                acc[key] = str(val)
                changed = True
        if ident.get("nickname") and acc.get("name") != ident.get("nickname"):
            # 只在没有自定义名字时同步，别覆盖用户自己起的备注
            acc.setdefault("_dy_nickname", ident.get("nickname"))
        if changed:
            if callable(self._save_config):
                try:
                    self._save_config(self.config)
                except Exception as e:
                    logger.warning("[%s] 抖音身份回填落盘失败: %r" % (self.account_id, e))
            if self._fetcher is not None:
                self._fetcher.owner = self.owner()   # 归属键升级，fetcher 要跟着换
            logger.info("[%s] 抖音身份已获取 uid=%s sec_uid=%s…"
                        % (self.account_id, acc.get("_dy_uid"),
                           str(acc.get("_dy_sec_uid") or "")[:16]))
        self._identity = ident
        return ident

    # ---------------- 抓取 ----------------
    async def fetch_now(self, max_works=None):
        client, why = self._need_client()
        if client is None:
            return {"__ok": False, "__err": "not_ready", "__msg": why}
        if platforms.owner_is_fallback(self.owner()):
            # 第一次跑：先拿身份，归属键才不会是 acct:<id>
            await self.refresh_identity()
        if self._fetcher is None:
            return {"__ok": False, "__err": "not_ready", "__msg": "抓取器未初始化"}
        try:
            rep = await self._fetcher.fetch_all(max_works=max_works)
        except Exception as e:
            self._last_error = "%r" % (e,)
            logger.error("[%s] 抖音抓取异常: %r" % (self.account_id, e))
            return {"__ok": False, "__err": "exception", "__msg": "%r" % (e,)}
        rep["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._last_report = rep
        self._last_error = "" if rep.get("complete") else "本轮不完整（有请求失败）"
        if self.emit:
            try:
                await self.emit("dy_fetched", {"account_id": self.account_id,
                                               "new": rep.get("new"),
                                               "complete": rep.get("complete")})
            except Exception:
                pass
        return dict(rep, __ok=True)

    # ---------------- 写操作 ----------------
    async def _item_id_of(self, aweme_id):
        """明文 aweme_id -> 加密 item_id（写操作必须用加密串）。

        加密串自带前缀 `@`，所以「传进来的已经是加密串」可以直接判定并直用，
        不必再查一次映射 —— 既省一枪请求，也兼容历史脏数据
        （rev56g 之前子回复的 export_id 被错存成加密串）。
        """
        if not aweme_id:
            return "", "缺少作品 id（aweme_id）"
        if str(aweme_id).startswith("@"):
            return str(aweme_id), ""
        m = await self._client.map_item_ids([str(aweme_id)], max_pages=10)
        enc = m.get(str(aweme_id))
        if not enc:
            return "", "拿不到该作品的加密 item_id（作品可能已删除或不在列表前几页）"
        return enc, ""

    async def reply(self, comment_id, aweme_id, text):
        client, why = self._need_client()
        if client is None:
            return {"__ok": False, "__err": "not_ready", "__msg": why}
        enc, why2 = await self._item_id_of(aweme_id)
        if not enc:
            return {"__ok": False, "__err": "no_item_id", "__msg": why2}
        env = await client.reply_comment(comment_id, enc, text)
        if env.get("__ok"):
            try:
                self.storage.mark_replied(comment_id)
                self.storage.mark_own_comment_pending(self.owner(), str(aweme_id), text,
                                                      "dy_manual", self.account_id)
            except Exception as e:
                logger.warning("[%s] 抖音回复成功但本地登记失败: %r" % (self.account_id, e))
        return env

    async def delete_comment(self, comment_id, aweme_id):
        client, why = self._need_client()
        if client is None:
            return {"__ok": False, "__err": "not_ready", "__msg": why}
        enc, why2 = await self._item_id_of(aweme_id)
        if not enc:
            return {"__ok": False, "__err": "no_item_id", "__msg": why2}
        env = await client.delete_comment(comment_id, enc)
        if env.get("__ok"):
            try:
                self.storage.mark_deleted(comment_id)
            except Exception as e:
                logger.warning("[%s] 抖音删除成功但本地标记失败: %r" % (self.account_id, e))
        return env
