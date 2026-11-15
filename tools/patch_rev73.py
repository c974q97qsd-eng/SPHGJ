# -*- coding: utf-8 -*-
"""rev73 一次性补丁：写操作成功独立 INFO 日志（path/账号/via/errCode/耗时）。

同一文件改 6 处 -> 一律整文件读入、逐条 assert 唯一命中、一次写回 + 自校验。
保持原文件的换行风格（CRLF/LF），避免破坏基线校验。
"""
import io
import os

P = r"D:\SPHGJ\backend\api_client.py"

PATCHES = [
    # 1) 文件头说明
    ("""rev72: 写接口(回复/删除/置顶/隐藏)也默认纯 HTTP,不再为写操作开浏览器。
       账号隔离:同账号串行、多账号并行,见 account_lock()。
""",
     """rev72: 写接口(回复/删除/置顶/隐藏)也默认纯 HTTP,不再为写操作开浏览器。
       账号隔离:同账号串行、多账号并行,见 account_lock()。
rev73: 写成功逐条打 INFO(path/账号/via/errCode/接口耗时);读/写各自的
       「首次 HTTP 成功」标记独立 —— 读接口先成功不再吞掉写通道的日志。
""",
     "docstring"),

    # 2) __init__: 读写 flag 分开
    ("""        self._http_logged = False        # 首次 HTTP 成功打一条 INFO,线上据此确认在跑
""",
     """        # rev73: 读/写各自一个「首次成功」标记。旧版只有 _http_logged 一个,
        # 读接口(post_list)先成功就置 True,写操作永远打不出「首次写操作成功」,
        # 线上只能靠 delete_logs 反推写通道在不在跑。
        self._http_logged = False        # 兼容 rev50~72:任意 HTTP 成功都置 True
        self._http_read_logged = False   # 读首次日志
        self._http_write_logged = False  # 写首次日志(与读完全分开)
""",
     "init flags"),

    # 3) _post_impl: 量请求阶段耗时 + 记住通道
    ("""        resp = None
        if self._via_http(is_write):
            resp = await self._post_via_http(path, url, body, page_url, is_write=is_write)
        if resp is None:
""",
     """        resp = None
        # rev73: 耗时只量「请求阶段」,不含 _throttle 的随机等待,也不含 _post
        # 里账号锁的排队时间 —— 否则写接口看起来总像慢了 4~8s。
        t_req = time.perf_counter()
        _use_http = self._via_http(is_write)
        if _use_http:
            resp = await self._post_via_http(path, url, body, page_url, is_write=is_write)
        if resp is None:
""",
     "request timing"),

    # 4) 请求结束,算出耗时
    ("""        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
""",
     """        cost = time.perf_counter() - t_req
        # 失败退避 / 成功重置;写操作成功计数(持久化)
        if self._is_failure(resp):
""",
     "cost"),

    # 5) 成功分支: 逐条写成功 INFO
    ("""        else:
            self._on_success()
            # rev72: 零发包的本地错误(no_cookie / http_blocked)不算一次写操作,
""",
     """        else:
            self._on_success()
            # rev73: 写成功逐条留痕。必须排除 __err —— daily_write_limit /
            # no_cookie / http_blocked 都是「零发包的本地错误」,被 _is_failure
            # 豁免退避后会进这个 else,不能记成写成功。
            if is_write and isinstance(resp, dict) and not resp.get("__err"):
                _code = self._err_code(resp)
                logger.info(
                    "[api] 写操作成功 path=%s account=%s via=%s errCode=%s 耗时=%.2fs",
                    path, (self.account_id or self._lock_key() or "-"),
                    "http" if _use_http else "page",
                    _code if _code is not None else "none", cost)
            # rev72: 零发包的本地错误(no_cookie / http_blocked)不算一次写操作,
""",
     "write success log"),

    # 6) _post_via_http: 读写首次日志分开
    ("""        # 有一次成功就说明会话是活的,清掉失效标记
        self.auth_fail = None
        if not self._http_logged:
            self._http_logged = True
            kind = "写" if is_write else "读"
            logger.info(f"[api] HTTP 直连可用(首次{kind}操作成功 path={path}, "
                        f"backend={wx_http.backend_name()}),{kind}接口走进程内请求")
        return resp
""",
     """        # 有一次成功就说明会话是活的,清掉失效标记
        self.auth_fail = None
        # rev73: 读写分开。旧逻辑一个 flag 共用,读接口先成功后写通道永远静默。
        # _http_logged 保留为兼容标记(旧单测/外部会读它),并继续抑制读首次日志。
        _compat = getattr(self, "_http_logged", False)
        self._http_logged = True
        if is_write:
            if not getattr(self, "_http_write_logged", False):
                self._http_write_logged = True
                logger.info(f"[api] HTTP 直连可用(首次写操作成功 path={path}, "
                            f"backend={wx_http.backend_name()}),写接口走进程内请求")
        elif not _compat and not getattr(self, "_http_read_logged", False):
            self._http_read_logged = True
            logger.info(f"[api] HTTP 直连可用(首次读操作成功 path={path}, "
                        f"backend={wx_http.backend_name()}),读接口走进程内请求")
        return resp
""",
     "first-success flags"),
]


def main():
    raw = io.open(P, "rb").read()
    crlf = raw.count(b"\r\n") > 0
    s = raw.decode("utf-8")
    if crlf:
        s = s.replace("\r\n", "\n")

    for old, new, tag in PATCHES:
        n = s.count(old)
        assert n == 1, "补丁 %s 命中 %d 次(应为 1)" % (tag, n)
        s = s.replace(old, new, 1)

    out = s.replace("\n", "\r\n") if crlf else s
    compile(out, P, "exec")
    io.open(P, "w", encoding="utf-8", newline="").write(out)

    # 立刻自校验
    chk = io.open(P, encoding="utf-8").read()
    assert "rev73: 写成功逐条打 INFO" in chk
    assert chk.count("_http_write_logged") >= 3
    assert chk.count("_http_read_logged") >= 2
    assert "写操作成功 path=%s account=%s via=%s errCode=%s 耗时=%.2fs" in chk
    assert "t_req = time.perf_counter()" in chk
    assert "cost = time.perf_counter() - t_req" in chk
    assert "首次写操作成功 path=" in chk
    assert "if not self._http_logged:" not in chk
    print("SELFCHECK PASS  bytes=%d crlf=%s patches=%d" %
          (len(out), crlf, len(PATCHES)))


if __name__ == "__main__":
    main()
