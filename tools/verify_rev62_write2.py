# -*- coding: utf-8 -*-
"""rev62 加强验证:page=None 时,**带合法 exportId** 的写请求能否被服务端正常受理。

做法:pin_comment(合法 exportId, commentId="0") ——
  commentId="0" 在服务端查不到对应评论,不会真的置顶任何东西;
  但请求本身带着合法的 exportId 走完整条写链路,响应能区分:
    - 修复前:{"__err": "no_page"}(根本没出本机)
    - 修复后:errCode / 业务错误(请求真的到达服务端并被处理)
"""
import asyncio
import json
import sys

sys.path.insert(0, r"D:\SPHGJ")
from backend.api_client import WxApiClient   # noqa: E402

ACC = "uspoloassn7"
# 取自上一步 post_list 的真实作品(10-30 发布),仓库/账号都是自己的
EXPORT = ("export/UzFfBgAAxImnADMiV1WGk8zT4DCaPnCzPzDTj-UgHC2NJA37LQ")


def main():
    cfg = json.load(open(r"\\Sphgj\sphgj\config.json", encoding="utf-8"))
    acc = [a for a in cfg["accounts"] if a["id"] == ACC][0]
    api = WxApiClient(None, acc, cfg, None)
    api._throttle = lambda p: asyncio.sleep(0)

    r = asyncio.run(api.pin_comment(EXPORT, "0"))
    print("resp =", str(r)[:300])
    err = str((r or {}).get("__err") or "")
    print("no_page 出现:", "no_page" in err)
    print("RESULT", "FAIL(还是没出本机)" if "no_page" in err else "PASS(请求已到服务端)")


if __name__ == "__main__":
    main()
