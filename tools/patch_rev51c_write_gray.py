# -*- coding: utf-8 -*-
r"""
rev51c: 写接口按账号灰度切 HTTP

为什么不能直接全量切：
    读接口 HTTP 失败 -> 回退浏览器重试是**幂等**的，安全。
    写接口不一样：HTTP 超时时服务端可能已经把评论发出去了，回退浏览器会**再发一次**
    -> 重复评论 / 重复删除 / 重复置顶。这是灰度最大的坑，比 TLS 指纹风险更实。

所以这里做两件事：

1) 灰度名单
   risk_control.api_write_http_accounts = ["uspoloassn1", "<account_id>", ...]
   名单内账号的写操作走 HTTP，其余仍走浏览器。默认空 = 全部走浏览器（现状不变）。
   填 _log_finder_id（视频号短名）或 account id 都能匹配。

2) 写操作的失败语义与读操作**不同**
   - no_cookie   : 请求根本没发出去（本地无登录态）-> 可安全回退浏览器
   - 超时/连接断  : 请求可能已被服务端处理 -> **绝不回退**，如实返回错误
   - errCode!=0   : 服务端明确拒绝（风控/会话失效）-> **绝不回退**，重试只会更糟
   不回退时返回带 __err 的 dict，让上层按失败处理（会触发退避，这是期望行为）。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "backend", "api_client.py")

PATCHES = [
    # ---------- 1. __init__ 解析灰度名单 ----------
    (
        r'''        self._read_via = str(rc.get("api_read_via") or "http").lower()
        self._write_via = str(rc.get("api_write_via") or "browser").lower()''',
        r'''        self._read_via = str(rc.get("api_read_via") or "http").lower()
        self._write_via = str(rc.get("api_write_via") or "browser").lower()
        # rev51c: 写操作灰度名单。填 account id 或视频号短名都行,默认空 = 全走浏览器。
        #   risk_control.api_write_http_accounts = ["uspoloassn1"]
        _g = rc.get("api_write_http_accounts") or []
        if isinstance(_g, str):
            _g = [x.strip() for x in _g.split(",") if x.strip()]
        self._write_http_accounts = {str(x) for x in _g}''',
        "解析写操作灰度名单",
    ),
    # ---------- 2. _via_http 支持按账号灰度 ----------
    (
        r'''    def _via_http(self, is_write):
        """本次走 HTTP 还是浏览器。读默认 http,写默认 browser。"""
        if time.time() < self._http_block_until:
            return False
        want = self._write_via if is_write else self._read_via
        return want == "http"''',
        r'''    def _account_keys(self):
        """可用于灰度匹配的账号标识集合(id / 视频号短名 / aid)。"""
        acc = self._latest_account() or {}
        keys = {
            str(self.account_id or ""),
            str(acc.get("id") or ""),
            str(acc.get("_log_finder_id") or ""),
            str(self.finder_id or ""),
            str(acc.get("_aid") or ""),
        }
        return {k for k in keys if k}

    def _via_http(self, is_write):
        """本次走 HTTP 还是浏览器。

        读: 默认 http(_read_via)。
        写: 默认 browser;全局 api_write_via="http" 或账号命中灰度名单才走 http。
        """
        if time.time() < self._http_block_until:
            return False
        if not is_write:
            return self._read_via == "http"
        if self._write_via == "http":
            return True
        if not self._write_http_accounts:
            return False
        return bool(self._account_keys() & self._write_http_accounts)''',
        "_via_http 支持按账号灰度",
    ),
    # ---------- 3. _http_give_up 加写操作语义 ----------
    (
        r'''    def _http_give_up(self, why, path, long_block=False):
        """HTTP 不可用 -> 记录原因并让调用方回退浏览器。"""
        if long_block:
            # cookie 不会在 3 分钟内自己长回来,别每轮白跑一次 HTTP
            self._http_block_until = time.time() + 180
        if self._http_last_why != why:
            self._http_last_why = why
            logger.info(f"[api] HTTP 直连不可用({why}),回退浏览器 path={path}"
                        + ("(180s 内不再尝试)" if long_block else ""))
        return None''',
        r'''    def _http_give_up(self, why, path, long_block=False, is_write=False):
        """HTTP 不可用 -> 记录原因。

        读操作: 返回 None,调用方回退浏览器重取(幂等,安全)。
        写操作: 只有 no_cookie(请求根本没发出去)可以安全回退;
                其余情况一律**不回退**,返回带 __err 的 dict ——
                HTTP 超时时服务端可能已经处理了,重发会造成重复评论/重复删除。
        """
        if long_block:
            # cookie 不会在 3 分钟内自己长回来,别每轮白跑一次 HTTP
            self._http_block_until = time.time() + 180
        if is_write and why != "no_cookie":
            logger.warning(f"[api] 写操作 HTTP 失败({why}) path={path} -> "
                           f"不重试(避免重复提交),按失败处理")
            return {"__err": f"http_{why}", "__no_retry": True, "path": path}
        if self._http_last_why != why:
            self._http_last_why = why
            logger.info(f"[api] HTTP 直连不可用({why}),回退浏览器 path={path}"
                        + ("(180s 内不再尝试)" if long_block else ""))
        return None''',
        "_http_give_up 加写操作语义",
    ),
    # ---------- 4. _post_via_http 透传 is_write ----------
    (
        r'''    async def _post_via_http(self, path, url, body, page_url):
        """进程内 HTTP 直连。返回 dict;None = 走不通,请回退浏览器。

        只认「成功」。任何非 0 errCode 都不当作业务失败 —— 直接退浏览器重发一次,
        因为浏览器里是活的 sessionid,能给出真正可信的答案。
        """''',
        r'''    async def _post_via_http(self, path, url, body, page_url, is_write=False):
        """进程内 HTTP 直连。返回 dict;None = 走不通,请回退浏览器。

        读操作: 只认「成功」,任何异常都退浏览器重试(幂等)。
        写操作: 见 _http_give_up —— 除 no_cookie 外一律不重试,避免重复提交。
        """''',
        "_post_via_http 签名加 is_write",
    ),
    (
        r'''        try:
            resp = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=30)
        except Exception as e:
            return self._http_give_up("exception:" + repr(e)[:60], path)
        err = resp.get("__err") if isinstance(resp, dict) else None
        if err == "no_cookie":
            return self._http_give_up("no_cookie", path, long_block=True)
        if err:
            return self._http_give_up(err, path)
        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。都不算"业务失败",交回浏览器
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333))''',
        r'''        try:
            resp = await wx_http.post_json(acc, url, body, page_url=page_url, timeout=30)
        except Exception as e:
            return self._http_give_up("exception:" + repr(e)[:60], path,
                                      is_write=is_write)
        err = resp.get("__err") if isinstance(resp, dict) else None
        if err == "no_cookie":
            # 本地就没有登录态,请求必然没发出去 -> 写操作也能安全回退
            return self._http_give_up("no_cookie", path, long_block=True)
        if err:
            return self._http_give_up(err, path, is_write=is_write)
        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333),
                                      is_write=is_write)''',
        "失败分支全部透传 is_write",
    ),
    # ---------- 5. 调用点传 is_write ----------
    (
        r'''        resp = None
        if self._via_http(is_write):
            resp = await self._post_via_http(path, url, body, page_url)''',
        r'''        resp = None
        if self._via_http(is_write):
            resp = await self._post_via_http(path, url, body, page_url, is_write=is_write)''',
        "_post 调用点传 is_write",
    ),
    # ---------- 6. 首次成功日志区分读/写 ----------
    (
        r'''        if not self._http_logged:
            self._http_logged = True
            logger.info(f"[api] HTTP 直连可用(首次成功 path={path}, "
                        f"backend={wx_http.backend_name()}),读接口改走进程内请求")''',
        r'''        if not self._http_logged:
            self._http_logged = True
            kind = "写" if is_write else "读"
            logger.info(f"[api] HTTP 直连可用(首次{kind}操作成功 path={path}, "
                        f"backend={wx_http.backend_name()}),{kind}接口走进程内请求")''',
        "首次成功日志区分读写",
    ),
    # ---------- 7. 文件头说明补灰度 ----------
    (
        r'''rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py),写接口默认仍走浏览器。''',
        r'''rev50: **读接口走进程内 HTTP 直连**(backend/wx_http.py),写接口默认仍走浏览器。
rev51c: 写接口支持按账号灰度(
        risk_control.api_write_http_accounts=["uspoloassn1"]),
        且写操作的 HTTP 失败**不自动重试**(避免重复评论),详见 _http_give_up。''',
        "文件头补说明",
    ),
]


def main():
    src = io.open(TARGET, encoding="utf-8").read()
    orig = src

    for old, new, tag in PATCHES:
        n = src.count(old)
        if n != 1:
            print("[FAIL] %s: 命中 %d 次（应为 1）" % (tag, n))
            return 1
        src = src.replace(old, new, 1)
        print("[OK] %s" % tag)

    if src == orig:
        print("[FAIL] 没有改动")
        return 1

    io.open(TARGET, "w", encoding="utf-8", newline="").write(src)
    print("\napi_client.py 已更新：%+d 字节" % (len(src) - len(orig)))

    import ast
    try:
        ast.parse(src)
        print("语法检查 OK")
    except SyntaxError as e:
        print("语法检查 FAIL:", e)
        return 1

    must = [
        "api_write_http_accounts",
        "def _account_keys",
        "is_write and why != \"no_cookie\"",
        "__no_retry",
        "is_write=is_write",
    ]
    bad = [m for m in must if m not in src]
    print("SELFCHECK", "PASS" if not bad else "FAIL -> %s" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
