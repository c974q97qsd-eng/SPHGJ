# -*- coding: utf-8 -*-
"""rev25-4c: 发布支持演练模式(dry_run) —— 上传 + 填描述,但不点发表。

用途:首次上线灰度。真发布是不可逆的对外动作,先用演练确认
「打开页面 / 注入视频 / 等转码 / 填描述 / 发表按钮可点」全部打通,
再决定要不要真发。
"""
import io, sys

P = r"D:\SPHGJ\backend\publish_queue.py"
s = io.open(P, encoding="utf-8").read()

OLD1 = '    async def run_task(self, task_id, manual=False):\n        """执行一条发布。返回结果 dict(同时写库)。"""'
NEW1 = ('    async def run_task(self, task_id, manual=False, dry_run=False):\n'
        '        """执行一条发布。返回结果 dict(同时写库)。\n\n'
        '        dry_run=True:只上传+填描述,不点发表、不归档文件,状态回到 pending,\n'
        '        用于首次灰度确认选择器与链路(不会产生真实作品)。\n'
        '        """')
assert s.count(OLD1) == 1, "run_task 锚点不唯一"
s = s.replace(OLD1, NEW1, 1)

OLD2 = "            res = await pub.publish(t, on_step=on_step)\n"
NEW2 = "            res = await pub.publish(t, on_step=on_step, dry_run=dry_run)\n"
assert s.count(OLD2) == 1
s = s.replace(OLD2, NEW2, 1)

OLD3 = """            if res.get("ok"):
                self.storage.set_task_status("""
NEW3 = """            if res.get("ok") and dry_run:
                # 演练:不归档、不算成功,任务回到 pending 等真发
                self.storage.set_task_status(
                    int(task_id), status="pending", step="dry_run",
                    error=res.get("error") or "演练完成:已上传并填好描述,未点发表",
                    account_id=w.account.get("id"), finished=False)
                logger.info(f"[publish] 演练通过 {t.get('file_name')}: 上传+填描述 OK,未发表")
                return res

            if res.get("ok"):
                self.storage.set_task_status("""
assert s.count(OLD3) == 1
s = s.replace(OLD3, NEW3, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---------------- server 传参 ----------------
PS = r"D:\SPHGJ\backend\server.py"
s2 = io.open(PS, encoding="utf-8").read()
OLD4 = '''@app.post("/api/publish/tasks/{task_id}/run")
async def publish_run(task_id: int):'''
NEW4 = '''@app.post("/api/publish/tasks/{task_id}/run")
async def publish_run(task_id: int, dry_run: bool = False):
    """立即发这一条。dry_run=1 只演练(上传+填描述,不点发表)。"""'''
assert s2.count(OLD4) == 1
s2 = s2.replace(OLD4, NEW4, 1)
OLD5 = "    res = await publish_queue.run_task(task_id, manual=True)"
NEW5 = "    res = await publish_queue.run_task(task_id, manual=True, dry_run=dry_run)"
assert s2.count(OLD5) == 1
s2 = s2.replace(OLD5, NEW5, 1)
io.open(PS, "w", encoding="utf-8", newline="").write(s2)

chk = io.open(P, encoding="utf-8").read() + io.open(PS, encoding="utf-8").read()
miss = [m for m in ("dry_run=False", "dry_run=dry_run", "dry_run: bool = False") if m not in chk]
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
