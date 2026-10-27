"""rev49: 直播探测「周期性完整检测」可关闭（live_probe_full_every=0 生效）。

背景（用户 2026-10-28 决策）:
  原实现 full_every = max(1, int(config.live_probe_full_every or 5)) + interval 120s
  => 每 10 分钟给**每个账号**开一次 Chromium + goto 直播大屏 + 等 4 秒。
  8 个账号 = 每小时 48 次浏览器启停，而这些账号全都不开播 -> 纯浪费（用户原话「收益是零」）。

  但轻量 HTTP 探测（1.1s/次、零浏览器、顺带维持 sessionid）不该一起停，
  所以改成: full_every 支持 0 = 永不周期性落完整检测，默认值改为 0。
  probe["live"] 为 True 时仍走 _open_full() 打开大屏页 —— 开播功能不减。

用法: python tools/patch_rev49_live_full.py
"""
import io
import sys

P = r"D:\SPHGJ\backend\account_manager.py"

PATCHES = [
    (
        '''            # 未播:轻量探测不开大屏页,直接收工。但每 N 次(默认 5)落一次完整检测,
            # 让 DOM 文本这条"接口不认、页面认"的兜底判据仍有出场机会。
            full_every = max(1, int(self.config.get("live_probe_full_every", 5)))
            if self._probe_since_full >= full_every:''',
        '''            # 未播:轻量探测不开大屏页,直接收工。
            # rev49(2026-10-28 用户决策 live_probe_full_every 默认 0):**永不周期性落完整检测**。
            # 原默认 5 + interval 120s = 每 10 分钟给每个账号开一次 Chromium + goto 大屏页 +
            # 等 4 秒;8 个账号每小时 48 次浏览器启停,而这些账号全都不开播 —— 纯浪费(用户原话
            # "收益是零")。关掉后未播时只有 1.1 秒的纯 HTTP 请求,浏览器一次都不开;
            # 真开播时 probe["live"] 走上面的 _open_full(),大屏页照样打开,功能不减。
            # 需要恢复 DOM 文本兜底判据时把它设成 >0(每 N 次未播落一次完整检测)。
            full_every = int(self.config.get("live_probe_full_every", 0) or 0)
            if full_every > 0 and self._probe_since_full >= full_every:''',
        "full_every 支持 0 禁用 + 默认改 0",
    ),
    (
        '''            # INFO 而非 DEBUG:轻量探测是 rev47 的核心链路,上线初期必须能在 run.log
            # 直接看到它的节拍(每账号默认 300s 一条),确认它在跑而不是在空转。
            logger.info(f"[{self.account['id']}] 轻量探测:未播(依据={probe['reason']},"
                        f"status={probe.get('status')}, via={probe.get('via')}, "
                        f"第 {self._probe_since_full}/{full_every} 次)")''',
        '''            # INFO 而非 DEBUG:轻量探测是 rev47 的核心链路,必须在 run.log 直接看到它的节拍,
            # 确认它在跑而不是在空转。rev49: full_every=0 时不再显示无意义的 N/0 计数。
            note = (f"第 {self._probe_since_full}/{full_every} 次"
                    if full_every > 0 else "不做完整检测")
            logger.info(f"[{self.account['id']}] 轻量探测:未播(依据={probe['reason']},"
                        f"status={probe.get('status')}, via={probe.get('via')}, {note})")''',
        "未播日志不再打印 N/0",
    ),
]


def main():
    s = io.open(P, encoding="utf-8").read()
    for old, new, tag in PATCHES:
        n = s.count(old)
        if n != 1:
            print(f"FAIL [{tag}] 命中 {n} 次，应为 1 次")
            return 1
        s = s.replace(old, new, 1)
        print(f"OK   [{tag}]")
    # 关键:full_every 的默认值必须真的是 0
    checks = [
        ('int(self.config.get("live_probe_full_every", 0) or 0)' in s, "默认值已改为 0"),
        ('full_every > 0 and self._probe_since_full >= full_every' in s, "0 时跳过完整检测"),
        ('max(1, int(self.config.get("live_probe_full_every", 5)))' not in s, "旧 max(1,..) 已移除"),
        ('if probe["live"]:' in s, "在播仍走 _open_full（功能未减）"),
        ('return await self._open_full()' in s, "_open_full 退化路径保留"),
    ]
    bad = [d for ok, d in checks if not ok]
    if bad:
        print("SELFCHECK FAIL:", bad)
        return 1
    io.open(P, "w", encoding="utf-8", newline="").write(s)
    print("SELFCHECK PASS")
    print("written:", P)
    return 0


if __name__ == "__main__":
    sys.exit(main())
