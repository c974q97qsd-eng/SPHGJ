# -*- coding: utf-8 -*-
"""rev52 step4: api_client 支持"无浏览器"运行
  1) http_available(): 本地有没有可用于直连的登录态(不访问网络)
  2) _post_via_page(): page 为 None 时安全返回,不再抛 AttributeError
"""
import io
import sys

P = r"D:\SPHGJ\backend\api_client.py"
s = io.open(P, encoding="utf-8").read()
orig = s
PATCHES = []

PATCHES.append((
    '''    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
        js = """''',
    '''    def http_available(self):
        """本地有没有可用于 HTTP 直连的登录态。不访问网络,纯查 config。

        rev52: 抓取流程要靠它决定「这轮要不要开浏览器」。有 storage_state 就先走纯
        HTTP(零 Chromium);没有(或已过期)才走 ensure_browser 那条老路。
        """
        try:
            return bool(wx_http.cookie_header_of(self._latest_account()))
        except Exception:
            return False

    async def _post_via_page(self, path, url, body):
        """原路径:页面内 fetch(同源、带真 cookie)。"""
        if self.page is None:
            # rev52: 纯 HTTP 模式(未开浏览器)下的回退请求。不能抛异常 ——
            # 抛出去会让整轮抓取炸掉,而正确语义只是「这条走不通」。
            return {"__err": "no_page"}
        js = """''',
    "http_available + page 保护",
))

for old, new, tag in PATCHES:
    if s.count(old) != 1:
        print("FAIL 命中 %d 次: %s" % (s.count(old), tag))
        sys.exit(1)
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

for ok, tag in [
    ("def http_available" in s, "http_available"),
    ('return {"__err": "no_page"}' in s, "no_page 保护"),
    (s.count("async def _post_via_page") == 1, "函数唯一"),
]:
    print(("PASS " if ok else "FAIL ") + tag)
print("bytes %d -> %d" % (len(orig.encode()), len(s.encode())))
