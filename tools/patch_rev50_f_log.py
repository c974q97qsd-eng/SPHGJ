# -*- coding: utf-8 -*-
"""rev50 步骤6: HTTP 直连首次成功时打一条 INFO（否则线上无法确认它真在跑）。"""
import io
import sys

P = r"D:\SPHGJ\backend\api_client.py"
s = io.open(P, encoding="utf-8").read()

OLD1 = '''        self._http_block_until = 0.0     # no_cookie 时短期禁用,别每轮白跑
        self._http_last_why = ""'''
NEW1 = '''        self._http_block_until = 0.0     # no_cookie 时短期禁用,别每轮白跑
        self._http_last_why = ""
        self._http_logged = False        # 首次 HTTP 成功打一条 INFO,线上据此确认在跑'''
assert s.count(OLD1) == 1
s = s.replace(OLD1, NEW1, 1)

OLD2 = '''        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。都不算"业务失败",交回浏览器
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333))
        return resp'''
NEW2 = '''        code = self._err_code(resp)
        if code not in (0, None):
            # 300333=会话失效,其它=风控/未知。都不算"业务失败",交回浏览器
            return self._http_give_up("errCode=%s" % code, path,
                                      long_block=(code == 300333))
        if not self._http_logged:
            self._http_logged = True
            logger.info(f"[api] HTTP 直连可用(首次成功 path={path}, "
                        f"backend={wx_http.backend_name()}),读接口改走进程内请求")
        return resp'''
assert s.count(OLD2) == 1
s = s.replace(OLD2, NEW2, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = io.open(P, encoding="utf-8").read()
for m in ("self._http_logged = False", "HTTP 直连可用(首次成功"):
    assert m in chk, m
print("OK  SELFCHECK PASS")
