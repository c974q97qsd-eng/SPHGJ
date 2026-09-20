# -*- coding: utf-8 -*-
"""账号选择页逻辑自检(mock 页面,无需扫码)。

覆盖:
  1. 负例:二维码登录页必须判定为「非选择页」(防止把二维码页误判,上次事故)
  2. 正例:选择页能被识别,且账号枚举完整(含滚动懒加载的第 4+ 个)
  3. 点击:按 index / 按 name 都能命中正确行,并自动点确认按钮
  4. 名称解析:name / role 拆分正确
"""
import asyncio
import os
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright  # noqa: E402
from backend.browser import launch_stealth, close_context_safely  # noqa: E402
from backend.login_capture import LoginSession  # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)


# ---------- mock 页面 ----------
QR_PAGE = """<html><body>
<div class="login__type__container__qrcode"><iframe src="about:blank" width="208" height="208"></iframe></div>
<div>登录视频号助手</div><div>使用微信扫码登录</div>
</body></html>"""

NAMES = ["示范店·甲仓", "示范店·乙仓", "示范店·丙仓", "示范店·丁仓"]
ROLES = ["超级管理员", "管理员", "运营者", "管理员"]


def _row(i):
    return (f'<div class="account-item" id="acc{i}" style="width:520px;height:64px">'
            f'<span class="checkbox"></span>'
            f'<img class="avatar" src="about:blank" width="40" height="40">'
            f'<div class="account-name">{NAMES[i]}</div>'
            f'<div class="account-role">{ROLES[i]}</div></div>')


def select_page(n, lazy_after=3):
    """前 lazy_after 个直接渲染,其余在滚动到底部时插入(模拟懒加载)。

    页面必须真的能滚动,否则 scrollBy 不触发 scroll 事件(首次实现在此踩坑)。
    """
    import json
    first = "".join(_row(i) for i in range(min(n, lazy_after)))
    rest = "".join(_row(i) for i in range(lazy_after, n))
    return f"""<html><body>
<h2>选择视频号登录</h2>
<div class="account-list" id="list">{first}</div>
<div id="spacer" style="height:1500px"></div>
<button id="confirm">登录</button>
<script>
  const REST = {json.dumps(rest)};
  let loaded = false;
  window.addEventListener('scroll', () => {{
    if (loaded) return;
    if (window.scrollY + window.innerHeight >= document.body.scrollHeight - 30) {{
      loaded = true;
      document.getElementById('list').insertAdjacentHTML('beforeend', REST);
      document.querySelectorAll('.account-item').forEach(el => {{
        el.addEventListener('click', () => {{ window.__clicked = el.id; }});
      }});
    }}
  }});
  document.querySelectorAll('.account-item').forEach(el => {{
    el.addEventListener('click', () => {{ window.__clicked = el.id; }});
  }});
  document.getElementById('confirm').onclick = () => {{ window.__confirmed = 1; }};
</script>
</body></html>"""


async def load(page, html):
    """用 data URL 加载 mock 页面。

    不能用 page.set_content():它只在首次调用时执行内联 <script>,后续调用会静默
    跳过,导致 click 处理器未注册而误报失败(实测踩坑)。
    """
    await page.goto("data:text/html;charset=utf-8," + quote(html))


async def new_session(page):
    s = LoginSession(None, {}, (lambda *a, **k: asyncio.sleep(0)))
    s.page = page
    s.status = "selecting_account"
    return s


async def stub_finalize(s):
    """截断抓取落盘:mock 页上没有真实的评论页,让它空转。"""

    async def _noop():
        return None

    s._capture_fields_and_finalize = _noop
    return s


async def main():
    d = tempfile.mkdtemp(prefix="_selftst_")
    async with async_playwright() as pw:
        ctx = await launch_stealth(pw, d, headless=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # ---- 1. 负例:二维码登录页 ----
        print("\n[1] 负例:二维码登录页不得误判为选择页")
        await load(page, QR_PAGE)
        s = await new_session(page)
        txt = await s._body_text()
        check(not s._is_select_page_text(txt), "二维码登录页 -> 非选择页")

        # ---- 2. 正例:识别 + 枚举(4 个,含懒加载) ----
        print("\n[2] 正例:选择页识别与账号枚举")
        await load(page, select_page(4))
        s = await new_session(page)
        txt = await s._body_text()
        check(s._is_select_page_text(txt), "选择页 -> 判定为选择页")
        accs = await s._enumerate_select_accounts()
        check(len(accs) == 4, f"枚举到 4 个账号(实际 {len(accs)})")
        got_names = [a["name"] for a in accs]
        check(got_names == NAMES, f"账号名与顺序正确: {got_names}")
        check([a["role"] for a in accs] == ROLES, f"角色解析正确: {[a['role'] for a in accs]}")
        check(getattr(s, "_select_names", None) == NAMES, "_select_names 已缓存(供 select_account 用)")

        # ---- 3. 点击:按 name ----
        print("\n[3] 点击:按 name 命中正确行")
        await load(page, select_page(4))
        await page.evaluate("window.__clicked = null")
        s = await new_session(page)
        ok = await s._click_select_account(2, "示范店·丙仓")
        clicked = await page.evaluate("window.__clicked")
        check(ok and clicked == "acc2", f"点 name=示范店·丙仓 -> acc2(实际 {clicked})")

        # ---- 4. 点击:仅按 index(不传 name) ----
        print("\n[4] 点击:仅按 index")
        await load(page, select_page(4))
        await page.evaluate("window.__clicked = null")
        s = await new_session(page)
        ok = await s._click_select_account(1)
        clicked = await page.evaluate("window.__clicked")
        check(ok and clicked == "acc1", f"点 index=1 -> acc1(实际 {clicked})")

        # ---- 5. 只 1 个账号的边界 ----
        print("\n[5] 边界:只有 1 个账号")
        await load(page, select_page(1))
        s = await new_session(page)
        accs = await s._enumerate_select_accounts()
        check(len(accs) == 1 and accs[0]["name"] == NAMES[0], f"单账号枚举: {[a['name'] for a in accs]}")

        # ---- 6. 无勾选框的布局(退回策略B/C) ----
        print("\n[6] 无勾选框布局:应退回 class / 文本行策略")
        await load(page, """<html><body><h2>选择视频号登录</h2>
        <div class="account-card" style="width:520px;height:60px">示范店·甲仓 管理员</div>
        <div class="account-card" style="width:520px;height:60px">示范店·丙仓 运营者</div>
        </body></html>""")
        s = await new_session(page)
        accs = await s._enumerate_select_accounts()
        check(len(accs) >= 1, f"无勾选框也能枚举(得到 {[a['name'] for a in accs]})")

        # ---- 7. 确认按钮 ----
        print("\n[7] 确认按钮点击")
        await load(page, select_page(4))
        s = await new_session(page)
        await s._click_select_confirm()
        confirmed = await page.evaluate("window.__confirmed")
        check(confirmed == 1, f"确认按钮被点击(__confirmed={confirmed})")

        # ---- 8. select_account():前端回调主路径(按名字查表点击) ----
        print("\n[8] select_account():按 names 缓存查表点击")
        await load(page, select_page(4))
        await page.evaluate("window.__clicked = null")
        s = await stub_finalize(await new_session(page))
        s._select_names = NAMES  # 模拟 _enumerate_select_accounts 的缓存
        ok = await s.select_account(2)
        clicked = await page.evaluate("window.__clicked")
        check(ok and clicked == "acc2",
              f"select_account(2) -> acc2(ok={ok} clicked={clicked})")
        check(s.status == "scanned", f"状态推进到 scanned(实际 {s.status})")

        # ---- 9. 状态守卫:非 selecting_account 时不得点击 ----
        print("\n[9] 状态守卫")
        await load(page, select_page(4))
        await page.evaluate("window.__clicked = null")
        s = await stub_finalize(await new_session(page))
        s._select_names = NAMES
        s.status = "waiting_scan"
        ok = await s.select_account(0)
        clicked = await page.evaluate("window.__clicked")
        check(ok is False and clicked is None,
              f"waiting_scan 下拒绝选择(ok={ok} clicked={clicked})")

        # ---- 10. 越界 index:不崩、明确失败 ----
        print("\n[10] 越界 index")
        await load(page, select_page(4))
        s = await stub_finalize(await new_session(page))
        s._select_names = NAMES
        ok = await s.select_account(99)
        check(ok is False, f"index=99 超出范围 -> False(实际 {ok})")

        await close_context_safely(ctx, d, "[selftest]")

    print(f"\n{'=' * 46}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    for f in FAIL:
        print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
