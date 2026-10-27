"""抖音（creator.douyin.com 创作者中心）适配层。

  api.py     接口规格：URL / body / 成功判定 / 归一化。纯函数，可离线单测。
  client.py  DouyinClient：节流、退避、写上限、统一返回信封，只依赖 page.evaluate。

为什么单独成包而不是塞进 api_client.py：
视频号客户端（backend/api_client.py）的每个方法名和参数都是 channels.weixin.qq.com
的形状（lastBuff / exportId / errCode），抖音是 max_cursor / item_id / status_code，
硬合成一个类会到处 if platform==。分开成两个客户端、共用上层抓取引擎的做法，
需要抽象的是「抓取策略」，不是「请求拼装」。
"""

from . import api  # noqa: F401
from .client import DouyinClient, PageTransport  # noqa: F401

__all__ = ["api", "DouyinClient", "PageTransport"]
