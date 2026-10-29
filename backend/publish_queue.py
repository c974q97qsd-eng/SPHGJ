"""自动发作品:inbox 扫描 + 队列调度 + 归档。

链路:
  你把视频丢进 inbox/<视频号目录>/  →  60s 扫描(认领视频号 + 取一条标题 + 指纹去重入队)
  →  调度器每 30s tick,按策略挑一条(全局串行 / 单号日限 / 随机间隔 / 时间窗 / 登录态门槛)
  →  VideoPublisher 用该视频号的登录卡片做 UI 发布  →  回读校验  →  归档 + 触发作品刷新

归属一律 finder_id(视频号),与作品/评论同口径;account_id 只记最后一次执行它的登录卡片。

三个安全阀:
  1. 幂等:publish_tasks 的 (finder_id,file_fp) 唯一键,同一文件永不重复入队
  2. 不自动重发:进程中断留下的 running 任务启动时转 need_review,由人工核对后再决定
  3. 熔断:同一视频号连续失败 2 次自动停发,避免硬撞风控

配置(config.json 的 publish 节点,全部有默认值):
  enabled / inbox_dir / scan_interval_sec / daily_limit / min_gap_sec / gap_jitter_sec
  window_start / window_end / auto_start_account / min_login_remain_sec
  mappings: {"目录名": "finder_id"}   # 目录 -> 视频号 的显式绑定
  per_finder: {"finder_id": {"daily_limit":.., "min_gap_sec":..}}  # 单号覆盖
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import shutil
import time
from datetime import datetime, timedelta

from .publisher import VideoPublisher

logger = logging.getLogger("sphgj")

VIDEO_EXTS = (".mp4", ".m4v", ".mov")
SUB_DONE = "_done"
SUB_FAILED = "_failed"
SUB_UNKNOWN = "_unknown"

DEFAULTS = {
    "enabled": False,              # 默认关:装好先看清配置再开
    "inbox_dir": "",               # 空 = <项目>/data/publish_inbox
    "scan_interval_sec": 60,
    "daily_limit": 1,
    "min_gap_sec": 2400,           # 同号两次发布最小间隔 40min
    "gap_jitter_sec": 3000,        # 再叠加 0~50min 随机抖动 -> 实际 40~90min
    "window_start": "08:00",
    "window_end": "22:30",
    "auto_start_account": True,    # 账号没运行就自动拉起并保持运行
    "min_login_remain_sec": 7200,  # 登录态剩余不足 2h 不发(避免发一半掉登录)
    "tick_interval_sec": 30,
    "refresh_posts_after_sec": 180,  # 发完多久触发一次作品列表刷新
    # 发布成功后把素材重命名为 "发布日期"发布-"标题"-原文件名
    # (rev41:归档到 <店铺目录>/_done/,不再一律塞 inbox/_done/)
    "rename_after_publish": True,
}


def file_fp(path):
    """快速指纹:size + 首 1MB + 尾 1MB 的 md5。大文件也能秒算,且足够防重。"""
    size = os.path.getsize(path)
    h = hashlib.md5(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def _hhmm_now():
    return datetime.now().strftime("%H:%M")


_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_name(s):
    """清掉 Windows 文件名非法字符(换成空格)并压缩连续空格。"""
    s = _BAD_NAME_CHARS.sub(" ", s or "")
    return re.sub(r"\s{2,}", " ", s).strip()


def _unique_path(path):
    """目标已存在时加 (2)(3) 序号,绝不覆盖已有归档文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
    return f"{stem} (dup){ext}"


def _in_window(start, end, now=None):
    """时间窗判断,支持跨午夜(如 22:00-02:00)。"""
    now = now or _hhmm_now()
    if not start or not end:
        return True
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


class PublishQueue:
    def __init__(self, manager, storage, config, emit=None, save_config=None):
        self.manager = manager
        self.storage = storage
        self.config = config
        self.emit = emit
        self._save_config = save_config
        self._task = None
        self._running = False
        self._busy = False                  # 全局串行:同一时刻只发一条
        self._last_scan_at = 0.0
        self._last_scan_stat = {}
        self._fail_streak = {}              # finder_id -> 连续失败次数(熔断用)
        self._last_tick = 0.0
        self._current = None                # 正在发的任务 id
        self._unbound = []                  # 认不出归属的目录(引导用户绑定)

    # ---------------- 重启前等待(rev58) ----------------
    def busy_now(self):
        """是否有作品正在发布:队列忙碌标志 + 库里 running 状态,两者取或。

        只认其中一个会漏:刚接到任务时标志先立、状态后写;异常退出后进程重启,
        标志归位但库里还留着 running。
        """
        if self._busy:
            return True
        try:
            return bool(self.storage.query_tasks(status="running", limit=1)[1])
        except Exception:
            return False

    async def wait_idle(self, max_wait=180.0, sleep=2.0):
        """等「当前这一条」发完再返回,返回实际等待秒数。

        为什么只等一条:发布本来就是全局串行的,进度都落在库里,重启后能接着发,
        排队中的任务不会丢。等到 max_wait 仍未结束也返回(由调用方决定强走)。
        """
        waited = 0.0
        while waited < max_wait:
            await asyncio.sleep(sleep)
            waited += sleep
            if not self.busy_now():
                break
        return waited

    # ---------------- 配置 ----------------
    @property
    def cfg(self):
        c = (self.config.get("publish") or {})
        out = dict(DEFAULTS)
        out.update({k: v for k, v in c.items() if v is not None})
        return out

    def _cfg_get(self, key):
        return self.cfg.get(key, DEFAULTS.get(key))

    @property
    def inbox_dir(self):
        d = (self._cfg_get("inbox_dir") or "").strip()
        if d:
            return d
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "data", "publish_inbox")

    def _per_finder(self, fid, key):
        per = (self.config.get("publish") or {}).get("per_finder") or {}
        v = ((per.get(fid) or {}).get(key))
        return v if v is not None else self._cfg_get(key)

    def _save_cfg_if_possible(self):
        try:
            if self._save_config:
                self._save_config(self.config)
        except Exception as e:
            logger.warning(f"[publish] 保存配置失败: {e}")

    async def _push(self):
        if not self.emit:
            return
        try:
            await self.emit("publish_status", self.status())
        except Exception:
            pass

    # ---------------- 视频号识别 ----------------
    def _finder_candidates(self):
        """[(finder_id, 账号名, 简称)] —— 只收有 finder_id 的账号。"""
        out = []
        for acc in self.config.get("accounts") or []:
            fid = (acc.get("locked_finder_id") or acc.get("_log_finder_id") or "").strip()
            if not fid:
                continue
            name = (acc.get("name") or "").strip()
            short = name.split(".")[-1].strip() if "." in name else name
            out.append((fid, name, short))
        return out

    def match_finder(self, dirname):
        """目录名 -> (finder_id, 依据)。依据: mapping / auto / ambiguous / unknown。"""
        dn = (dirname or "").strip()
        if not dn:
            return "", "unknown"
        mp = (self.config.get("publish") or {}).get("mappings") or {}
        if dn in mp and mp[dn]:
            return mp[dn], "mapping"
        cands = []
        for fid, name, short in self._finder_candidates():
            if dn == name or (short and dn == short):
                cands.append(fid)
            elif dn == fid or (len(dn) >= 6 and fid.endswith(dn)):
                cands.append(fid)
        uniq = list(dict.fromkeys(cands))
        if len(uniq) == 1:
            # 唯一命中即记住,下次不必再模糊匹配
            mp[dn] = uniq[0]
            self.config.setdefault("publish", {})["mappings"] = mp
            self._save_cfg_if_possible()
            return uniq[0], "auto"
        return "", ("ambiguous" if uniq else "unknown")

    def finder_label(self, fid):
        for f, name, short in self._finder_candidates():
            if f == fid:
                return name or short or fid[-8:]
        return "视频号 " + (fid or "")[-8:] if fid else "未知视频号"

    # ---------------- 扫描 inbox ----------------
    def scan_inbox(self):
        """扫 inbox,给新视频建任务。返回统计 dict(同步函数,操作很轻)。"""
        stat = {"dirs": 0, "files": 0, "queued": 0, "dup": 0, "no_title": 0,
                "unbound": [], "at": datetime.now().isoformat()}
        root = self.inbox_dir
        if not os.path.isdir(root):
            self._last_scan_stat = stat
            self._last_scan_at = time.time()
            return stat

        unbound = []
        for name in sorted(os.listdir(root)):
            sub = os.path.join(root, name)
            if not os.path.isdir(sub):
                continue
            # _done/_failed/_unknown 等辅助目录都不是视频号目录,一律跳过
            if name.startswith("_") or name.startswith("."):
                continue
            stat["dirs"] += 1
            fid, how = self.match_finder(name)
            videos = [f for f in sorted(os.listdir(sub))
                      if os.path.isfile(os.path.join(sub, f))
                      and os.path.splitext(f)[1].lower() in VIDEO_EXTS
                      and not f.startswith(".")]
            if not videos:
                continue
            stat["files"] += len(videos)
            if not fid:
                unbound.append({"dir": name, "files": len(videos), "reason": how})
                continue
            for fn in videos:
                p = os.path.join(sub, fn)
                try:
                    fp = file_fp(p)
                    size = os.path.getsize(p)
                except Exception as e:
                    logger.warning(f"[publish] 读文件失败 {p}: {e}")
                    continue
                # 已入过队(含已成功/失败)的一律跳过,幂等
                tid, created = self.storage.enqueue_task(fid, p, fn, size, fp)
                if not created:
                    stat["dup"] += 1
                    continue
                # 取一条标题(一条一用);没标题就先挂着,等补标题后自动补上
                title_id, title = self.storage.take_title(fid)
                if title_id:
                    self.storage.update_task_title(tid, title)
                    self._set_title_id(tid, title_id)
                    self.storage.bind_title_task(title_id, tid)
                else:
                    stat["no_title"] += 1
                stat["queued"] += 1

        self._unbound = unbound
        stat["unbound"] = unbound
        self._last_scan_stat = stat
        self._last_scan_at = time.time()
        if stat["queued"] or unbound:
            logger.info(f"[publish] 扫描完成: 新入队 {stat['queued']} / 重复 {stat['dup']} / "
                        f"缺标题 {stat['no_title']} / 未认领目录 {len(unbound)}")
        return stat

    def _set_title_id(self, task_id, title_id):
        c = self.storage._conn()
        try:
            c.execute("UPDATE publish_tasks SET title_id=? WHERE id=?", (int(title_id), int(task_id)))
            c.commit()
        finally:
            c.close()

    def fill_missing_titles(self):
        """给「已入队但没标题」的 pending 任务补标题(标题池新加了内容之后调用)。"""
        rows, _ = self.storage.query_tasks(status="pending", limit=500)
        n = 0
        for t in rows:
            if (t.get("title") or "").strip():
                continue
            tid, content = self.storage.take_title(t["finder_id"])
            if not tid:
                break
            self.storage.update_task_title(t["id"], content)
            self._set_title_id(t["id"], tid)
            self.storage.bind_title_task(tid, t["id"])
            n += 1
        return n

    # ---------------- 调度 ----------------
    def can_run(self, task, manual=False):
        """这条任务现在能不能发。返回 (ok, 原因)。manual=True 只查硬条件。"""
        fid = task.get("finder_id") or ""
        now = time.time()

        # 人工停过的账号:不自动拉起(除非手动点发)
        acc_id, _ = self._account_for_finder(fid)
        if acc_id and acc_id in getattr(self.manager, "human_stopped", set()) and not manual:
            return False, f"账号 {acc_id} 已被你手动停止,该视频号暂停自动发布"

        if not manual:
            if not _in_window(self._cfg_get("window_start"), self._cfg_get("window_end")):
                return False, "不在发布时间窗内"
            limit = int(self._per_finder(fid, "daily_limit") or 0)
            today = datetime.now().strftime("%Y-%m-%d")
            done = self.storage.daily_success_count(fid, today)
            if limit and done >= limit:
                return False, f"今日已发 {done}/{limit} 条,等明天"
            gap = float(self._per_finder(fid, "min_gap_sec") or 0)
            jitter = float(self._cfg_get("gap_jitter_sec") or 0)
            last = self.storage.last_success_at(fid)
            if last and gap:
                try:
                    last_ts = datetime.fromisoformat(last).timestamp()
                except Exception:
                    last_ts = 0
                need = gap + (random.uniform(0, jitter) if jitter else 0)
                if last_ts and now - last_ts < need:
                    left = int((need - (now - last_ts)) / 60)
                    return False, f"距上次发布不足间隔,还要等约 {left} 分钟"
            if self._fail_streak.get(fid, 0) >= 2:
                return False, "连续失败 2 次已自动暂停该视频号,请在发布记录里核对后手动重试"
        return True, ""

    def _account_for_finder(self, fid):
        """找一个「正登录着该视频号」的卡片 id。返回 (account_id, 是否在运行)。"""
        if not fid:
            return "", False
        workers = getattr(self.manager, "workers", {}) or {}
        for aid, w in workers.items():
            if (w.account.get("_log_finder_id") or "").strip() == fid:
                return aid, True
        for acc in self.config.get("accounts") or []:
            lf = (acc.get("locked_finder_id") or acc.get("_log_finder_id") or "").strip()
            if lf == fid:
                return acc.get("id") or "", False
        return "", False

    async def _ensure_worker(self, fid):
        """拿到能发该视频号的 worker。必要时自动拉起账号。返回 (worker, err)。"""
        aid, running = self._account_for_finder(fid)
        if not aid:
            return None, f"视频号 {self.finder_label(fid)} 没有绑定任何登录卡片,请先在账号管理里添加并登录"
        w = (getattr(self.manager, "workers", {}) or {}).get(aid)
        if w is not None:
            return w, ""
        if not self._cfg_get("auto_start_account"):
            return None, f"账号 {aid} 未运行(未开启自动拉起),请手动启动后再发"
        if aid in getattr(self.manager, "human_stopped", set()):
            return None, f"账号 {aid} 已被你手动停止,不会自动拉起"
        logger.info(f"[publish] 自动拉起账号 {aid} 用于发布")
        w = await self.manager.start_account(aid)
        if w is None:
            return None, f"自动启动账号 {aid} 失败"
        if not w.logged_in:
            return None, f"账号 {aid} 登录态失效,需要重新扫码(发布已排队等待)"
        return w, ""

    def _login_remain_sec(self, w):
        """登录态剩余秒数(24h TTL)。拿不到 login_at 时返回 None(不拦)。"""
        acc = (w.account or {})
        ts = (acc.get("login_at") or "").strip()
        if not ts:
            return None
        try:
            expire = datetime.fromisoformat(ts) + timedelta(hours=24)
            return (expire - datetime.now()).total_seconds()
        except Exception:
            return None

    async def run_task(self, task_id, manual=False, dry_run=False):
        """执行一条发布。返回结果 dict(同时写库)。

        dry_run=True:只上传+填描述,不点发表、不归档文件,状态回到 pending,
        用于首次灰度确认选择器与链路(不会产生真实作品)。
        """
        t = self.storage.get_task(int(task_id))
        if not t:
            return {"ok": False, "error": "任务不存在"}
        fid = t.get("finder_id") or ""
        if not (t.get("title") or "").strip():
            return {"ok": False, "error": "这条任务还没有标题(标题池为空),补标题后再发"}
        if not os.path.isfile(t.get("src_path") or ""):
            self.storage.set_task_status(int(task_id), status="failed",
                                         error="源视频文件不见了(可能被移动/删除)",
                                         step="precheck", finished=True)
            return {"ok": False, "error": "源视频文件不见了"}

        self._busy = True
        self._current = int(task_id)
        attempts = int(t.get("attempts") or 0) + 1
        self.storage.set_task_status(int(task_id), status="running", step="prepare",
                                     started=True, attempts=attempts,
                                     error="", object_id="", export_id="")
        await self._push()

        holder = False
        w = None
        try:
            w, err = await self._ensure_worker(fid)
            if err:
                self.storage.set_task_status(int(task_id), status="pending" if "重新扫码" in err else "failed",
                                             step="prepare", error=err, finished="重新扫码" not in err)
                self._fail_streak[fid] = self._fail_streak.get(fid, 0) + 1
                return {"ok": False, "error": err}

            # 登录身份必须与任务归属一致,否则会把作品发错号
            cur = (w.account.get("_log_finder_id") or "").strip()
            if cur and fid and cur != fid:
                err = f"账号 {w.account.get('id')} 当前登录的是别的视频号({cur[-8:]}),与任务归属不符"
                self.storage.set_task_status(int(task_id), status="failed", step="prepare",
                                             error=err, finished=True)
                return {"ok": False, "error": err}

            remain = self._login_remain_sec(w)
            min_remain = float(self._cfg_get("min_login_remain_sec") or 0)
            if remain is not None and min_remain and remain < min_remain:
                err = f"登录态只剩 {int(remain/60)} 分钟(<{int(min_remain/60)} 分钟),先重新扫码再发"
                self.storage.set_task_status(int(task_id), status="pending", step="prepare",
                                             error=err, finished=False)
                return {"ok": False, "error": err}

            ok = await w.ensure_browser()
            if not ok:
                self.storage.set_task_status(int(task_id), status="pending", step="prepare",
                                             error="浏览器打不开,稍后重试", finished=False)
                return {"ok": False, "error": "浏览器打不开"}
            holder = True

            async def on_step(name, detail=""):
                self.storage.set_task_status(int(task_id), step=name)
                await self._push()

            pub = VideoPublisher(w.context, w.account, self.storage, self.config,
                                 api=w.api, emit=self.emit)
            res = await pub.publish(t, on_step=on_step, dry_run=dry_run)

            if res.get("ok") and dry_run:
                # 演练:不归档、不算成功,任务回到 pending 等真发
                self.storage.set_task_status(
                    int(task_id), status="pending", step="dry_run",
                    error=res.get("error") or "演练完成:已上传并填好描述,未点发表",
                    account_id=w.account.get("id"), finished=False)
                logger.info(f"[publish] 演练通过 {t.get('file_name')}: 上传+填描述 OK,未发表")
                return res

            if res.get("ok"):
                self.storage.set_task_status(
                    int(task_id), status="success", step="done",
                    error=res.get("error") or "", object_id=res.get("object_id") or "",
                    export_id=res.get("export_id") or "", account_id=w.account.get("id"),
                    finished=True)
                self._fail_streak[fid] = 0
                archived = self._archive(t, ok=True)
                if archived:
                    self.storage.set_task_status(int(task_id), archived_name=archived)
                logger.info(f"[publish] 发布成功 {t.get('file_name')} -> {self.finder_label(fid)}"
                            f" object_id={res.get('object_id')}"
                            + (f" | 已归档为 {archived}" if archived else ""))
                # 稍后触发一次作品列表刷新,让作品管理里能看到这条
                if self._cfg_get("refresh_posts_after_sec"):
                    asyncio.create_task(self._refresh_posts_later(w, int(task_id)))
            else:
                attempts_fail = self._fail_streak.get(fid, 0) + 1
                self._fail_streak[fid] = attempts_fail
                self.storage.set_task_status(
                    int(task_id), status="failed", step=res.get("step") or "",
                    error=res.get("error") or "", account_id=w.account.get("id"),
                    finished=True)
                # 没发出去 -> 标题还回池子,下次还能用
                if t.get("title_id"):
                    self.storage.release_title(t["title_id"])
                # 刻意不移动源文件:留着才能点「重试」;同 (finder,fp) 唯一键保证不会再入队
                logger.warning(f"[publish] 发布失败 {t.get('file_name')}: {res.get('error')}")
            return res
        except Exception as e:
            logger.exception("[publish] 发布任务异常")
            self.storage.set_task_status(int(task_id), status="failed", step="exception",
                                         error=f"{type(e).__name__}: {str(e)[:300]}",
                                         finished=True)
            return {"ok": False, "error": str(e)[:300]}
        finally:
            self._busy = False
            self._current = None
            if holder and w is not None:
                try:
                    await w.release_idle_browser()
                except Exception:
                    pass
            await self._push()

    def _archive_subdir(self, src, ok=True):
        """算出 src 该归档到哪个目录 —— 跟着**源文件所在的店铺目录**走。

        rev41:旧行为一律丢进 inbox/_done/(inbox 根下一级),而 scan_inbox 是把
        inbox/<目录名>/ 当店铺目录的,于是归档后素材不再按店铺区分,人工翻 _done
        看不出是哪家店发的。现在归到 <店铺目录>/_done/。

        返回 (目标目录, 是否贴合店铺目录)。退回 inbox/<_done|_failed> 的情形:
          * src 为空 / 不在当前 inbox 之下(改过 inbox_dir、历史别处的路径、跨盘)
            —— 绝不能因为源文件在别处就往用户别处建 _done 目录
          * src 直接在 inbox 根(不是店铺目录)
        src 已经在 _done/_failed/_unknown 里时返回其父目录本身(原地,视为已归档)。
        """
        root = os.path.abspath(self.inbox_dir)
        fallback = os.path.join(root, SUB_DONE if ok else SUB_FAILED)
        if not src:
            return fallback, False
        parent = os.path.dirname(os.path.abspath(src))
        try:
            inside = (os.path.normcase(os.path.commonpath([root, parent]))
                      == os.path.normcase(root))
        except ValueError:      # 不同盘符/UNC 不同共享 -> commonpath 抛 ValueError
            inside = False
        if not inside or os.path.normcase(parent) == os.path.normcase(root):
            return fallback, False
        name = os.path.basename(parent)
        if not name:
            return fallback, False
        if name.startswith("_") or name.startswith("."):
            # 已经在 _done/_failed/_unknown 等辅助目录里:原地即归档结果
            return parent, False
        return os.path.join(parent, SUB_DONE if ok else SUB_FAILED), True

    def _archive(self, task, ok=True):
        """把源文件移进 <店铺目录>/_done/。返回归档后的文件名(失败返回 "")。

        rev26:开启 rename_after_publish 时按用户要求的格式重命名:
            "发布日期"发布-"使用的标题"-原文件名
        例:2026-10-20发布-厚底老爹鞋视觉增高 比例一下就出来了-2月28日 (58).mp4
        rev41:归档目录跟着源文件所在店铺走(inbox/<店名>/_done/),不再一律塞
            inbox/_done/。店铺目录里没视频可扫时 scan_inbox 会跳过它,不影响认领。
        """
        src = task.get("src_path") or ""
        if not src or not os.path.isfile(src):
            return ""
        sub, in_shop = self._archive_subdir(src, ok)
        # 源文件已经在目标目录里(重复归档 / 已归档过) -> 原地就是结果,不再搬
        if os.path.normcase(os.path.dirname(os.path.abspath(src))) == os.path.normcase(sub):
            return os.path.basename(src)
        try:
            os.makedirs(sub, exist_ok=True)
            name = os.path.basename(src)
            if ok and self._cfg_get("rename_after_publish"):
                name = self._published_name(task, src, sub)
            dst = _unique_path(os.path.join(sub, name))
            shutil.move(src, dst)
            if not in_shop:
                # 退回到 inbox 根:源文件不在店铺目录下,留线索便于排查
                logger.warning(f"[publish] 归档退回到 inbox 根(源文件不在店铺目录下) "
                               f"{src} -> {dst}")
            return os.path.basename(dst)
        except Exception as e:
            logger.warning(f"[publish] 归档文件失败 {src}: {e}")
            return ""

    def _published_name(self, task, src, dest_dir=None):
        """拼 "日期发布-标题-原文件名"。标题清洗为合法文件名,整体长度受控。

        rev41:dest_dir 是实际归档目录(含店铺名)。260 路径上限要按它算,
        否则目标目录多一级店名后,长标题会拼出超长路径导致归档失败。
        """
        stem, ext = os.path.splitext(os.path.basename(src))
        # 原文件名一般合法,但外部拷入/网络来源可能带非法字符,一并清掉
        stem = _safe_name(stem) or "video"
        title = (task.get("title") or "").strip()
        # 标题里的换行/制表符压成空格,话题 # 保留(它是标题的一部分)
        title = re.sub(r"\s+", " ", title).strip()
        title = _safe_name(title)[:60].strip(" .-_")
        date = datetime.now().strftime("%Y-%m-%d")
        if title:
            name = f"{date}发布-{title}-{stem}{ext}"
        else:
            name = f"{date}发布-{stem}{ext}"
        # Windows 路径上限 260:超长时截断标题而不是截断原文件名(原名是溯源关键)
        room = 250 - len(dest_dir or os.path.join(self.inbox_dir, SUB_DONE))
        if len(name) > room:
            over = len(name) - room
            keep = max(8, len(title) - over - 1)
            name = f"{date}发布-{title[:keep].strip(' .-_')}-{stem}{ext}"
            if len(name) > room:
                name = f"{date}发布-{stem}{ext}"
        return name

    async def _refresh_posts_later(self, worker, task_id=None):
        """发完延迟触发一次作品列表刷新(让作品管理立刻能看到新作品)。

        刷新完顺手把 object_id 回填到任务:视频还在转码时 verify 回读不到,
        等作品真正出现在列表里再补上,任务页就不会一直空着作品 ID。
        """
        await asyncio.sleep(float(self._cfg_get("refresh_posts_after_sec") or 180))
        try:
            from .post_fetcher import PostFetcher
            aid = worker.account.get("id")
            if aid not in (getattr(self.manager, "workers", {}) or {}):
                return
            if not await worker.ensure_browser():
                return
            try:
                await PostFetcher(self.storage, worker.account, self.config).refresh(worker)
                logger.info(f"[publish] 发布后刷新作品列表完成 ({aid})")
            finally:
                await worker.release_idle_browser()
        except Exception as e:
            logger.debug(f"[publish] 发布后刷新作品列表失败: {e}")
            return
        if task_id:
            try:
                self._backfill_object_id(int(task_id))
            except Exception as e:
                logger.debug(f"[publish] 回填作品 ID 失败: {e}")

    def _backfill_object_id(self, task_id):
        """作品已进作品表时,按「视频号 + 标题前 20 字」把 object_id 补回任务。"""
        t = self.storage.get_task(int(task_id))
        if not t or (t.get("object_id") or ""):
            return
        want = re.sub(r"\s+", "", (t.get("title") or ""))[:20]
        if not want:
            return
        rows, _ = self.storage.query_posts(finder_id=t.get("finder_id"), limit=30)
        for p in rows or []:
            if want in re.sub(r"\s+", "", p.get("title") or ""):
                self.storage.set_task_status(
                    int(task_id), object_id=p.get("object_id") or "",
                    export_id=p.get("export_id") or "", error="")
                logger.info(f"[publish] 回填作品 ID 成功 task={task_id} "
                            f"object_id={p.get('object_id')}")
                self.storage.set_task_status(int(task_id), step="done")
                return

    # ---------------- 后台循环 ----------------
    def start(self):
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[publish] 调度器已启动")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
            self._task = None
        logger.info("[publish] 调度器已停止")

    async def _loop(self):
        await asyncio.sleep(20)  # 等启动流程(自动恢复账号)先跑完
        while self._running:
            try:
                if self._cfg_get("enabled"):
                    now = time.time()
                    if now - self._last_scan_at >= float(self._cfg_get("scan_interval_sec") or 60):
                        self.scan_inbox()
                        self.fill_missing_titles()
                        await self._push()
                    if not self._busy:
                        await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[publish] 调度循环异常: {e}")
            await asyncio.sleep(float(self._cfg_get("tick_interval_sec") or 30))

    async def _tick(self):
        """挑一条可发的任务执行。全局串行:一次只发一条。"""
        rows, _ = self.storage.query_tasks(status="pending", limit=50)
        for t in rows:
            if not (t.get("title") or "").strip():
                continue
            ok, why = self.can_run(t)
            if not ok:
                if why and t["id"] == rows[0]["id"]:
                    logger.debug(f"[publish] 跳过 {t['id']}: {why}")
                continue
            logger.info(f"[publish] 开始发布任务 {t['id']} ({t.get('file_name')})")
            await self.run_task(t["id"])
            return

    # ---------------- 前端状态 ----------------
    def status(self):
        pending = self.storage.query_tasks(status="pending")[1]
        running = self.storage.query_tasks(status="running")[1]
        need_review = self.storage.query_tasks(status="need_review")[1]
        today = datetime.now().strftime("%Y-%m-%d")
        today_done = {}
        for fid, _name, _short in self._finder_candidates():
            n = self.storage.daily_success_count(fid, today)
            if n:
                today_done[fid] = n
        return {
            "enabled": bool(self._cfg_get("enabled")),
            "inbox_dir": self.inbox_dir,
            "busy": self._busy,
            "current_task": self._current,
            "pending": pending,
            "running": running,
            "need_review": need_review,
            "today_success": today_done,
            "unbound": self._unbound,
            "fail_streak": dict(self._fail_streak),
            "last_scan": self._last_scan_stat,
            "in_window": _in_window(self._cfg_get("window_start"), self._cfg_get("window_end")),
            "cfg": {k: self._cfg_get(k) for k in DEFAULTS},
        }
