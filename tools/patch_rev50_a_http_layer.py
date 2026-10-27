# -*- coding: utf-8 -*-
"""rev50 步骤1: live_probe_http 改为复用 wx_http 的 HTTP 原语（去重复代码）。

删掉本地实现: _cookie_domain_ok / build_cookie_header / _post_httpx / _post_urllib /
_request / backend_name，改为从 wx_http 导入（名字保持 build_cookie_header，
account_manager 的调用点不受影响）。
"""
import io
import sys

P = r"D:\SPHGJ\backend\live_probe_http.py"

s = io.open(P, encoding="utf-8").read()
orig = len(s)

# 1) 顶部 import
OLD_IMPORT = '''import asyncio
import json
import os
import re
import time

from .live_fetcher import LiveFetcher, _load_spec_cache, judge_live

try:
    import httpx as _httpx
    HAS_HTTPX = True
except Exception:            # 干净克隆的机器可能还没装
    _httpx = None
    HAS_HTTPX = False
'''
NEW_IMPORT = '''import json
import os
import re
import time

from .live_fetcher import LiveFetcher, _load_spec_cache, judge_live
# rev50: HTTP 原语统一搬到 wx_http(依赖图的叶子节点),这里只保留探测业务逻辑。
# 名字原样 re-export, extern 调用点(live_probe_http.build_cookie_header)无需改动。
from .wx_http import (        # noqa: F401
    HAS_HTTPX,
    backend_name,
    build_cookie_header,
    request_raw,
)
'''
assert s.count(OLD_IMPORT) == 1, "import 段未唯一命中"
s = s.replace(OLD_IMPORT, NEW_IMPORT, 1)

# 2) 删 _cookie_domain_ok + build_cookie_header
i = s.index("def _cookie_domain_ok(dom):")
j = s.index("def load_live_status_spec(account_id):")
assert i < j
s = s[:i] + s[j:]

# 3) 删 _post_httpx / _post_urllib / _request
i = s.index("async def _post_httpx(")
j = s.index("def _dump_live_response(")
assert i < j
s = s[:i] + s[j:]

# 4) 删 backend_name
i = s.index("def backend_name():")
s = s[:i].rstrip() + "\n"

# 5) 调用点换成 request_raw（注意参数顺序不同: method, url, ...）
OLD_CALL = "        code, txt = await _request(url, method, headers, body)"
NEW_CALL = "        code, txt = await request_raw(method, url, headers, body, DEFAULT_TIMEOUT)"
assert s.count(OLD_CALL) == 1, "调用点未唯一命中"
s = s.replace(OLD_CALL, NEW_CALL, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

# 自校验
chk = io.open(P, encoding="utf-8").read()
must = [
    "from .wx_http import",
    "await request_raw(method, url, headers, body, DEFAULT_TIMEOUT)",
    "async def probe_http(",
    "def prepare_request(",
]
must_not = [
    "async def _post_httpx(",
    "async def _post_urllib(",
    "def _cookie_domain_ok(",
    "await _request(",
    "import httpx as _httpx",
]
bad = [m for m in must if m not in chk] + [m for m in must_not if m in chk]
if bad:
    print("SELFCHECK FAIL:", bad)
    sys.exit(1)
print("OK  %d -> %d 字节 (-%d)" % (orig, len(chk), orig - len(chk)))
print("SELFCHECK PASS")
