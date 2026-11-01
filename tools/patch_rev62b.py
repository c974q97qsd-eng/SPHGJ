# -*- coding: utf-8 -*-
"""rev62b: HTTP 写失败时补打完整响应(rev61 承诺过,但只覆盖了浏览器那条路)。"""
import io

P = r"D:\SPHGJ\backend\api_client.py"
s = io.open(P, encoding="utf-8").read()

old = """            if code in (300333, 300330):
                self.auth_fail = {"code": code, "ts": time.time(), "path": path}
            # 读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,"""
new = """            if code in (300333, 300330):
                self.auth_fail = {"code": code, "ts": time.time(), "path": path}
            if is_write:
                # rev62: 写操作经 HTTP 直连失败时,响应在这里就被 _http_give_up
                # 换成 {"__err": "http_errCode=..."} 了,上层再也看不到原始报文。
                # rev61 要求「写失败必须能看到服务端说了什么」,这里补上。
                logger.warning("[api] HTTP 写失败 path=%s code=%s resp=%s",
                               path, code, str(resp)[:400])
            # 读操作交回浏览器;写操作不重试
            return self._http_give_up("errCode=%s" % code, path,"""

assert s.count(old) == 1, "锚点不唯一: %d" % s.count(old)
s = s.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="").write(s)

ok = 'HTTP 写失败 path=%s code=%s resp=%s' in s
print("SELFCHECK", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
