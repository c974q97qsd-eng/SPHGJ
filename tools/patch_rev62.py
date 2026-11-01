# -*- coding: utf-8 -*-
"""rev62 补丁:纯 HTTP 模式下写操作直接走 HTTP,不再回退浏览器。

问题(2026-11-02 01:58 实测):
  [uspoloassn7] 自动评论失败 export/...: {'__err': 'no_page'}
  [api] 请求失败(连续 11 次),退避 600s

根因:
  _via_http(is_write=True) 在 api_write_via=browser 且账号不在灰度名单时返回 False
  -> _post 走 _post_via_page -> self.page is None(浏览器已收回,非直播常态)
  -> {"__err": "no_page"}。于是:
    1) 写操作一条都发不出去(该账号自动评论从未成功);
    2) no_page 被 _is_failure 当成接口失败 -> 连续失败触发 600s 全局退避,
       把同账号正常读请求一起停掉。
"""
import io

P = r"D:\SPHGJ\backend\api_client.py"
s = io.open(P, encoding="utf-8").read()
orig = s

PATCHES = []

# ---- 1) _via_http: 没有页面时写操作走 HTTP ----
PATCHES.append((
    """        if self._write_via == "http":
            return True
        if not self._write_http_accounts:
            return False""",
    """        if self._write_via == "http":
            return True
        # rev62: 纯 HTTP 模式(浏览器已收回 / 从未打开)下,写操作直接走 HTTP。
        # 原来这里返回 False -> _post_via_page -> page is None -> {"__err":"no_page"},
        # 于是 uspoloassn7 每轮「自动评论失败: no_page」,一条都没发出去;
        # 而且 no_page 被 _is_failure 当成接口失败,连续 11 次触发 600s 全局退避,
        # 把正常的读请求一起停掉。写操作不再为它去开浏览器:同源同 cookie。
        if self.page is None:
            return True
        if not self._write_http_accounts:
            return False""",
    "via_http_write_no_page",
))

# ---- 2) _post: 没有页面时不再返回 no_page,如实上报 HTTP 侧原因 ----
PATCHES.append((
    """        if resp is None:
            resp = await self._post_via_page(path, url, body)""",
    """        if resp is None:
            if self.page is None:
                # rev62: 没有页面就不存在"回退浏览器"这件事。原来这里一律调
                # _post_via_page,拿回来的 no_page 既看不出真实原因(no_cookie?
                # cookie 已过期?),又会被计成一次接口失败去触发退避。
                resp = {"__err": "no_page:" + (self._http_last_why or "unknown")}
            else:
                resp = await self._post_via_page(path, url, body)""",
    "post_no_page_reason",
))

# ---- 3) _is_failure: 本地走不通不算接口失败,别触发退避 ----
PATCHES.append((
    """        err = resp.get("__err")
        if err and err != "daily_write_limit":
            return True""",
    """        err = resp.get("__err")
        # rev62: daily_write_limit / no_cookie / no_page:* 都是「本地这条路走不通」,
        # 不是服务端拒绝。原来只有 daily_write_limit 豁免,no_cookie 也照样进退避,
        # 而退避(30/120/600s)是按账号的 —— 会把同账号的正常读请求一起停掉。
        if err and err != "daily_write_limit" and not (
                err == "no_cookie" or str(err).startswith("no_page")):
            return True""",
    "is_failure_local_err",
))

for old, new, tag in PATCHES:
    assert s.count(old) == 1, "锚点不唯一/未找到: %s (count=%d)" % (tag, s.count(old))
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# ---- 自校验 ----
ok = True
checks = [
    ("if self.page is None:\n            return True", True),
    ('"no_page:" + (self._http_last_why or "unknown")', True),
    ('str(err).startswith("no_page")', True),
    ("resp = await self._post_via_page(path, url, body)", True),  # 仍在 else 分支里
]
for needle, want in checks:
    got = needle in s
    print(("  OK   " if got == want else "  FAIL ") + needle[:56])
    ok = ok and (got == want)
print("SELFCHECK", "PASS" if ok else "FAIL", "| 字节 %d -> %d" % (len(orig), len(s)))
raise SystemExit(0 if ok else 1)
