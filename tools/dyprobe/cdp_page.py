# -*- coding: utf-8 -*-
"""把 CDP 客户端伪装成 Playwright 的 page —— 只为在**本机没装 playwright** 时
验证 backend/douyin/client.py 的真实行为。

生产环境不需要这个文件：直接把 worker.page（Playwright）传给 DouyinClient 即可，
两者对客户端而言是同一种东西（只有 `await page.evaluate(expr, arg)` 一个方法）。

一致性要点（和 Playwright 必须完全一样，否则验证结果没有参考价值）：
  · 表达式是 JS 函数，arg 作为**唯一实参**传入（Playwright 就是这么调用的）
  · awaitPromise=True，expression 返回 Promise 时等它 resolve
  · returnByValue=True，返回可 JSON 化的值
"""
import json


class CdpPage:
    """duck-typed page：只有 evaluate(expr, arg)。"""

    def __init__(self, cdp):
        self.cdp = cdp
        self.calls = 0

    async def evaluate(self, expression, arg=None):
        self.calls += 1
        if arg is None:
            js = "(%s)()" % expression
        else:
            js = "(%s)(%s)" % (expression, json.dumps(arg, ensure_ascii=False))
        return self.cdp.eval(js, await_promise=True)
