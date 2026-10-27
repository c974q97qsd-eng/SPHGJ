# -*- coding: utf-8 -*-
"""rev52 step2: api_client
  1) post_list pageSize 默认 20 -> 100(config.post_list_page_size 可调,上限 100)
  2) _is_failure 把 errCode 非 0 也算失败(原来只认 ret,errCode=-1 不触发退避)
  3) 新增 write_blocked(): 供上层在达每日写上限后直接跳过,不再逐条打 WARNING
"""
import io
import sys

P = r"D:\SPHGJ\backend\api_client.py"
s = io.open(P, encoding="utf-8").read()
orig = s
PATCHES = []

# 1) pageSize 默认 100
PATCHES.append((
    '''    async def fetch_video_list(self, last_buff="", only_unread=False, page_size=20):
        """视频列表。返回 {data:{list:[{objectId,commentCount,createTime,...}], ...}}。lastBuff 分页。"""
        return await self._post("post/post_list", self._body(
            pageSize=page_size, onlyUnread=only_unread, needAllCommentCount=True,
            userpageType=13, forMcn=False, lastBuff=last_buff))''',
    '''    async def fetch_video_list(self, last_buff="", only_unread=False, page_size=None):
        """视频列表。返回 {data:{list:[{objectId,commentCount,createTime,...}], ...}}。lastBuff 分页。

        rev52: pageSize 默认 20 -> 100。这是本项目请求量最大的一条路径 ——
        每轮都要翻完全部作品,pageSize=20 时 8 个账号合计 348 页/轮(白天 10 分钟一轮
        = 41760 次/天,占总请求 88%,单轮串行耗时 10.1 分钟已把 10 分钟的周期额度吃满)。
        实测 100 条/页可用:返回 100 条、errCode=0、约 1 秒、427KB,连翻 3 页无重复
        且时间连续(08-31 11:01 跨页正确衔接)。响应体大小与 20 条/页按比例放大,
        总流量不变,省的是请求数(348->70 页/轮)与节流等待。
        上限按 100 封顶:再大未实测,不拿线上账号试。
        """
        if page_size is None:
            try:
                page_size = int((self.config or {}).get("post_list_page_size", 100) or 100)
            except (TypeError, ValueError):
                page_size = 100
        page_size = max(1, min(100, page_size))
        return await self._post("post/post_list", self._body(
            pageSize=page_size, onlyUnread=only_unread, needAllCommentCount=True,
            userpageType=13, forMcn=False, lastBuff=last_buff))''',
    "pageSize 默认 100",
))

# 2) _is_failure 认 errCode
PATCHES.append((
    '''        if resp.get("ret") not in (None, 0):
            return True
        base = resp.get("base_resp")
        if isinstance(base, dict) and base.get("ret") not in (None, 0):
            return True
        return False''',
    '''        if resp.get("ret") not in (None, 0):
            return True
        base = resp.get("base_resp")
        if isinstance(base, dict) and base.get("ret") not in (None, 0):
            return True
        # rev52: 视频号接口的业务错误码在 errCode 上,原来这里只看 ret ——
        # errCode=-1 时被当成"成功",既不退避也不计数,于是连续被拒还照常猛打。
        # 线上实测 2 号重扫后 20 分钟内出现 4 次 errCode=-1(post_list/comment_list 各 2 次)。
        code = self._err_code(resp)
        if code not in (0, None):
            return True
        return False''',
    "_is_failure 认 errCode",
))

# 3) write_blocked()
PATCHES.append((
    '''    def _daily_count(self, today):''',
    '''    def write_blocked(self):
        """今日写操作是否已达上限。

        rev52: 供上层在每轮/每视频判断前先问一句,避免明知会被拒还逐条调用
        (原来 auto_comment 对每个新视频都 try_comment -> post_comment 被上限挡回,
        线上一天打了 5574 条 WARNING,把日志淹掉)。
        """
        today = time.strftime("%Y-%m-%d")
        try:
            return self._daily_count(today) >= self._daily_write_limit
        except Exception:
            return False

    def _daily_count(self, today):''',
    "write_blocked",
))

for old, new, tag in PATCHES:
    if s.count(old) != 1:
        print("FAIL 命中 %d 次: %s" % (s.count(old), tag))
        sys.exit(1)
    s = s.replace(old, new, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = [
    ("page_size=None):\n        \"\"\"视频列表" in s, "签名已改"),
    ("post_list_page_size" in s, "读配置"),
    ("min(100, page_size)" in s, "上限 100"),
    ("code = self._err_code(resp)\n        if code not in (0, None):" in s, "失败判定"),
    ("def write_blocked" in s, "write_blocked"),
]
for ok, tag in chk:
    print(("PASS " if ok else "FAIL ") + tag)
print("bytes %d -> %d" % (len(orig.encode()), len(s.encode())))
