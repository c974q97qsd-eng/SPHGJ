# -*- coding: utf-8 -*-
"""rev63: 修自动发布「已熔断」误判 + 熔断死锁。

按《母机指令-发布熔断误判修复.md》逐条落地，每条都 assert 唯一命中。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PQ = os.path.join(ROOT, "backend", "publish_queue.py")
SV = os.path.join(ROOT, "backend", "server.py")


def rd(p):
    return io.open(p, encoding="utf-8").read()


def wr(p, s):
    io.open(p, "w", encoding="utf-8", newline="").write(s)


def apply(path, patches):
    s = rd(path)
    for old, new, tag in patches:
        n = s.count(old)
        assert n == 1, "[%s] 命中 %d 次(应为 1): %s" % (tag, n, old[:70].replace("\n", "\\n"))
        s = s.replace(old, new, 1)
    wr(path, s)
    return len(patches)


# ---------------------------------------------------------------- publish_queue
PQ_PATCHES = [
    # 改动 1: 常量
    (
        "DEFAULTS = {\n",
        '# rev61: 「环境未就绪」错误前缀。带此前缀的失败不计入熔断 ——\n'
        '# 账号掉线等人扫码不是发布失败,计入会形成熔断死锁(详见 _ensure_worker)。\n'
        '_OFFLINE_TAG = "【需扫码】"\n\n\n'
        'DEFAULTS = {\n',
        "1 _OFFLINE_TAG",
    ),
    # 改动 7a: 配置项
    (
        '    "tick_interval_sec": 30,\n',
        '    "tick_interval_sec": 30,\n'
        '    # rev61: 熔断(同一视频号连败 2 次)后冷却多久自动半开试发一条。\n'
        '    # 0 = 永久熔断,只能手动点重试或重启进程(旧行为)。\n'
        '    "circuit_cooldown_sec": 1800,\n',
        "7a circuit_cooldown_sec",
    ),
    # 改动 4: _fail_at
    (
        "        self._fail_streak = {}              # finder_id -> 连续失败次数(熔断用)\n",
        "        self._fail_streak = {}              # finder_id -> 连续失败次数(熔断用)\n"
        "        self._fail_at = {}                  # rev61: finder_id -> 最后一次真失败时间戳(熔断半开用)\n",
        "4 _fail_at",
    ),
    # 改动 5: _bump_fail_streak
    (
        "    def can_run(self, task, manual=False):\n",
        "    def _bump_fail_streak(self, fid):\n"
        '        """记一次真失败。rev61: 同时打时间戳,供熔断冷却期满后自动半开试探。"""\n'
        "        self._fail_streak[fid] = self._fail_streak.get(fid, 0) + 1\n"
        "        self._fail_at[fid] = time.time()\n\n"
        "    def can_run(self, task, manual=False):\n",
        "5 _bump_fail_streak",
    ),
    # 改动 6: can_run 冷却 + 半开
    (
        '            if self._fail_streak.get(fid, 0) >= 2:\n'
        '                return False, "连续失败 2 次已自动暂停该视频号,请在发布记录里核对后手动重试"\n',
        '            streak = self._fail_streak.get(fid, 0)\n'
        '            if streak >= 2:\n'
        '                # rev61: 熔断加冷却期,到点自动半开试一条。\n'
        '                # 旧版是纯内存计数、无 TTL:熔断 -> 不再派任务 -> 永远没有成功 -> 永不归零,\n'
        '                # 只能重启或手动点重试,实测把两个号锁死了两天。\n'
        '                # 半开失败会再次 _bump_fail_streak 并刷新时间戳,自然进入下一轮冷却。\n'
        '                cool = float(self._cfg_get("circuit_cooldown_sec") or 0)\n'
        '                last_fail = float(self._fail_at.get(fid) or 0)\n'
        '                waited = (now - last_fail) if last_fail else 1e9\n'
        '                if cool > 0 and waited < cool:\n'
        '                    left = int((cool - waited) / 60) + 1\n'
        '                    return False, f"连续失败 {streak} 次已熔断,约 {left} 分钟后自动试发一次"\n'
        '                logger.info(f"[publish] {self.finder_label(fid)} 熔断冷却期已过,半开试发一条")\n',
        "6 can_run cooldown",
    ),
    # 改动 2: _ensure_worker 区分掉线与真失败
    (
        "        w = await self.manager.start_account(aid)\n"
        "        if w is None:\n"
        '            return None, f"自动启动账号 {aid} 失败"\n'
        "        if not w.logged_in:\n"
        '            return None, f"账号 {aid} 登录态失效,需要重新扫码(发布已排队等待)"\n',
        "        w = await self.manager.start_account(aid)\n"
        "        if w is None:\n"
        "            # rev61: start_account 返回 None 有两种完全不同的原因:\n"
        "            #   1) 已知掉线(_is_known_offline) -> 直接排队扫码,要人等,属于环境未就绪\n"
        "            #   2) 浏览器真起不来 -> 属于失败,该计入熔断\n"
        "            # 旧版一律算失败,队列每 30s tick 一次,两条任务就把号熔断并锁死。\n"
        "            acc = next((a for a in (self.config.get(\"accounts\") or [])\n"
        "                        if a.get(\"id\") == aid), None)\n"
        "            known_offline = bool(\n"
        "                acc is not None\n"
        "                and getattr(self.manager, \"_is_known_offline\", None)\n"
        "                and self.manager._is_known_offline(acc))\n"
        "            if known_offline:\n"
        '                return None, f"{_OFFLINE_TAG}账号 {aid} 登录态失效,已排队等待扫码(不计入熔断)"\n'
        '            return None, f"自动启动账号 {aid} 失败"\n'
        "        if not w.logged_in:\n"
        '            return None, f"{_OFFLINE_TAG}账号 {aid} 登录态失效,需要重新扫码(发布已排队等待)"\n',
        "2 _ensure_worker",
    ),
    # 改动 3: run_task 只有真失败才计数
    (
        "            w, err = await self._ensure_worker(fid)\n"
        "            if err:\n"
        '                self.storage.set_task_status(int(task_id), status="pending" if "重新扫码" in err else "failed",\n'
        '                                             step="prepare", error=err, finished="重新扫码" not in err)\n'
        "                self._fail_streak[fid] = self._fail_streak.get(fid, 0) + 1\n"
        '                return {"ok": False, "error": err}\n',
        "            w, err = await self._ensure_worker(fid)\n"
        "            if err:\n"
        "                # rev61: 「需扫码」不是发布失败 —— 任务留 pending 等下一条,且不计入熔断\n"
        '                need_login = err.startswith(_OFFLINE_TAG) or "重新扫码" in err\n'
        "                self.storage.set_task_status(int(task_id),\n"
        '                                             status="pending" if need_login else "failed",\n'
        "                                             step=\"prepare\", error=err, finished=not need_login)\n"
        "                if not need_login:\n"
        "                    self._bump_fail_streak(fid)\n"
        '                return {"ok": False, "error": err}\n',
        "3 run_task streak",
    ),
    # 改动 7b: 成功时清时间戳
    (
        "                self._fail_streak[fid] = 0\n",
        "                self._fail_streak[fid] = 0\n"
        "                self._fail_at.pop(fid, None)\n",
        "7b success clear",
    ),
    # 改动 7c(补充): 真发布失败分支也走 _bump,否则冷却计时器没起,
    # streak=2 时 last_fail=0 -> waited=1e9 -> 冷却被跳过,等于没修。
    (
        "                attempts_fail = self._fail_streak.get(fid, 0) + 1\n"
        "                self._fail_streak[fid] = attempts_fail\n",
        "                self._bump_fail_streak(fid)\n",
        "7c fail branch bump",
    ),
]

# ---------------------------------------------------------------- server.py
SV_PATCHES = [
    (
        '    publish_queue._fail_streak[t.get("finder_id") or ""] = 0\n'
        "    if t.get(\"status\") in (\"failed\", \"need_review\", \"skipped\"):\n",
        '    _fid = t.get("finder_id") or ""\n'
        "    publish_queue._fail_streak[_fid] = 0\n"
        "    publish_queue._fail_at.pop(_fid, None)\n"
        "    if t.get(\"status\") in (\"failed\", \"need_review\", \"skipped\"):\n",
        "S1 publish_run",
    ),
    (
        '    publish_queue._fail_streak[t.get("finder_id") or ""] = 0\n'
        "    # 标题被还回池子后可能已被别的任务用掉 -> 重新取一条\n",
        '    _fid = t.get("finder_id") or ""\n'
        "    publish_queue._fail_streak[_fid] = 0\n"
        "    publish_queue._fail_at.pop(_fid, None)\n"
        "    # 标题被还回池子后可能已被别的任务用掉 -> 重新取一条\n",
        "S2 publish_retry",
    ),
]


def main():
    n1 = apply(PQ, PQ_PATCHES)
    n2 = apply(SV, SV_PATCHES)
    print("publish_queue.py 应用 %d 处 / server.py 应用 %d 处" % (n1, n2))

    # ---- 改动 8: 新增解除接口，插在 publish_retry 函数末尾
    s = rd(SV)
    anchor = ("    storage.reset_task(task_id)\n"
              "    await publish_queue._push()\n"
              '    return {"ok": True}\n')
    assert s.count(anchor) == 1, "reset-streak 锚点命中 %d 次" % s.count(anchor)
    new_ep = (
        '    storage.reset_task(task_id)\n'
        "    await publish_queue._push()\n"
        '    return {"ok": True}\n'
        "\n\n"
        '@app.post("/api/publish/finders/{finder_id}/reset-streak")\n'
        "async def publish_reset_streak(finder_id: str):\n"
        '    """rev61: 手动解除某个视频号的熔断(连败计数清零,并清掉冷却时间戳)。"""\n'
        "    publish_queue._fail_streak[finder_id] = 0\n"
        "    publish_queue._fail_at.pop(finder_id, None)\n"
        "    await publish_queue._push()\n"
        '    logger.info(f"[publish] 人工解除熔断: {publish_queue.finder_label(finder_id)}")\n'
        '    return {"ok": True, "finder_id": finder_id}\n'
    )
    s = s.replace(anchor, new_ep, 1)
    wr(SV, s)
    print("server.py 新增 reset-streak 接口")

    # ---- 自校验
    pq = rd(PQ)
    sv = rd(SV)
    checks = [
        ("_OFFLINE_TAG 定义", '_OFFLINE_TAG = "【需扫码】"' in pq),
        ("OFFLINE_TAG 在 DEFAULTS 之前", pq.index("_OFFLINE_TAG") < pq.index("DEFAULTS = {")),
        ("circuit_cooldown_sec 默认值", '"circuit_cooldown_sec": 1800' in pq),
        ("_fail_at 初始化", "self._fail_at = {}" in pq),
        ("_bump_fail_streak 定义", "def _bump_fail_streak(self, fid):" in pq),
        ("_bump_fail_streak 在 can_run 之前",
         pq.index("def _bump_fail_streak") < pq.index("def can_run(self, task")),
        ("can_run 冷却判断", 'cool = float(self._cfg_get("circuit_cooldown_sec") or 0)' in pq),
        ("_ensure_worker 掉线分支", "已排队等待扫码(不计入熔断)" in pq),
        ("_ensure_worker logged_in 带 tag", '_OFFLINE_TAG}账号 {aid} 登录态失效,需要重新扫码' in pq),
        ("run_task need_login", 'need_login = err.startswith(_OFFLINE_TAG)' in pq),
        ("run_task 旧计数字已删",
         'status="pending" if "重新扫码" in err else "failed"' not in pq),
        ("成功清时间戳", "self._fail_at.pop(fid, None)" in pq),
        ("旧永久熔断文案已换", "连续失败 2 次已自动暂停该视频号" not in pq),
        ("server reset-streak", "/api/publish/finders/{finder_id}/reset-streak" in sv),
        ("server publish_run 清 _fail_at",
         sv.count("publish_queue._fail_at.pop(_fid, None)") == 2),
    ]
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(("  %-34s %s" % (n, "OK" if ok else "FAIL")))
    if bad:
        print("SELFCHECK FAIL:", bad)
        sys.exit(1)
    print("SELFCHECK PASS")


if __name__ == "__main__":
    main()
