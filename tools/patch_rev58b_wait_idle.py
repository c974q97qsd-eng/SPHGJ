"""rev58b:把「等发布任务结束」的逻辑下沉到 PublishQueue(busy_now / wait_idle),
便于单测,也避免重启等待判断散在 server 里。
"""
import io
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def patch(rel, patches):
    p = os.path.join(ROOT, rel)
    s = io.open(p, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, f"[{rel}:{tag}] 命中 {n} 次,期望 1 次"
        s = s.replace(old, new, 1)
        print(f"  ok  {rel}  {tag}")
    io.open(p, "w", encoding="utf-8", newline="").write(s)


# ---- 1. publish_queue:新增 busy_now / wait_idle ----
patch("backend/publish_queue.py", [
    ("busy_now / wait_idle",
     '''    # ---------------- 配置 ----------------
    @property
    def cfg(self):''',
     '''    # ---------------- 重启前等待(rev58) ----------------
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
    def cfg(self):'''),
])

# ---- 2. server:改用 publish_queue.busy_now / wait_idle ----
patch("backend/server.py", [
    ("去掉 _publish_busy,改调 queue",
     '''def _publish_busy():
    """是否有作品正在发布:队列忙碌标志 + 库里 running 状态,两者取或。

    只认其中一个会漏:进程刚接到任务时标志先立、状态后写;异常退出时又只剩状态。
    """
    try:
        if getattr(publish_queue, "_busy", False):
            return True
        return bool(storage.query_tasks(status="running", limit=1)[1])
    except Exception:
        return False


''',
     ''''''),
    ("endpoint 用 busy_now",
     '''    if wait_task and _publish_busy():''',
     '''    if wait_task and publish_queue.busy_now():'''),
    ("等待循环下沉",
     '''        async def _restart_after_task():
            waited = 0.0
            try:
                while waited < max_wait:
                    await asyncio.sleep(2.0)
                    waited += 2.0
                    if not _publish_busy():
                        break
                else:
                    logger.warning(f"[restart] 等了 {waited:.0f}s 发布仍未结束,"
                                   f"按 max_wait 强制重启;该任务重启后会被标成待核对")
                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                restart_mod.restart(delay)''',
     '''        async def _restart_after_task():
            try:
                waited = await publish_queue.wait_idle(max_wait=max_wait, sleep=2.0)
                if publish_queue.busy_now():
                    logger.warning(f"[restart] 等了 {waited:.0f}s 发布仍未结束,"
                                   f"按 max_wait 强制重启;该任务重启后会被标成待核对")
                logger.info(f"[restart] 发布任务已结束(等了 {waited:.0f}s),开始重启")
                restart_mod.restart(delay)'''),
])

# ---- 自校验 ----
q = io.open(os.path.join(ROOT, "backend/publish_queue.py"), encoding="utf-8").read()
assert "def busy_now(self):" in q and "async def wait_idle(self, max_wait=180.0, sleep=2.0):" in q
sv = io.open(os.path.join(ROOT, "backend/server.py"), encoding="utf-8").read()
assert "_publish_busy" not in sv
assert "publish_queue.busy_now()" in sv and "await publish_queue.wait_idle(" in sv
print("SELFCHECK PASS")
