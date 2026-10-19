# -*- coding: utf-8 -*-
"""rev26b: 发布后回填 object_id。

首发现象:32MB 视频点发表后 ~45s 内 post_list 首屏还没出现该作品(转码中),
verify 只能判 ok=True 但拿不到 object_id/export_id,任务页留一条误导性提示。

改法:
  - verify 重试 4 次 × 20s(总约 80s),给转码多一点时间
  - 发布后延迟刷新作品列表那一步(默认 180s)完成后再按标题回读一次,
    命中就把 object_id/export_id 写回任务,并清掉「稍后刷新」的临时提示
"""
import io
import os

LOCAL = r"D:\SPHGJ"
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# 1) verify 多给一点时间
add(r"backend\publisher.py",
    "    async def _verify_in_list(self, page, title, tries=3, gap=15):",
    "    async def _verify_in_list(self, page, title, tries=4, gap=20):",
    "publisher: verify 重试 4×20s")

# 2) 刷新时带上 task_id,便于回填
add(r"backend\publish_queue.py",
    """                if self._cfg_get("refresh_posts_after_sec"):
                    asyncio.create_task(self._refresh_posts_later(w))""",
    """                if self._cfg_get("refresh_posts_after_sec"):
                    asyncio.create_task(self._refresh_posts_later(w, int(task_id)))""",
    "queue: 刷新任务带 task_id")

add(r"backend\publish_queue.py",
    '''    async def _refresh_posts_later(self, worker):
        """发完延迟触发一次作品列表刷新(让作品管理立刻能看到新作品)。"""
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
            logger.debug(f"[publish] 发布后刷新作品列表失败: {e}")''',
    '''    async def _refresh_posts_later(self, worker, task_id=None):
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
        want = re.sub(r"\\s+", "", (t.get("title") or ""))[:20]
        if not want:
            return
        rows, _ = self.storage.query_posts(finder_id=t.get("finder_id"), limit=30)
        for p in rows or []:
            if want in re.sub(r"\\s+", "", p.get("title") or ""):
                self.storage.set_task_status(
                    int(task_id), object_id=p.get("object_id") or "",
                    export_id=p.get("export_id") or "", error="")
                logger.info(f"[publish] 回填作品 ID 成功 task={task_id} "
                            f"object_id={p.get('object_id')}")
                self.storage.set_task_status(int(task_id), step="done")
                return''',
    "queue: 刷新后回填 object_id")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel)
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")

    print("\n== 自校验 ==")
    import ast
    for f in ("publisher", "publish_queue"):
        src = io.open(os.path.join(LOCAL, "backend", f + ".py"), encoding="utf-8").read()
        ast.parse(src)
        print(f"  OK  AST {f}.py")
    q = io.open(os.path.join(LOCAL, "backend", "publish_queue.py"), encoding="utf-8").read()
    for needle, tag in (("def _backfill_object_id", "回填函数"),
                        ("self._refresh_posts_later(w, int(task_id))", "调用点"),
                        ('tries=4, gap=20', "") ):
        if tag:
            print(f"  {'OK ' if needle in q else 'BAD'} {tag}")
    pp = io.open(os.path.join(LOCAL, "backend", "publisher.py"), encoding="utf-8").read()
    print(f"  {'OK ' if 'tries=4, gap=20' in pp else 'BAD'} verify 参数")
    print("\n[DONE] rev26b 应用完成")


if __name__ == "__main__":
    apply()
