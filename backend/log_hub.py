"""内存日志中心:把 logging 输出收集到环形缓冲,实时推送到前端界面。

用途:软件以无控制台方式运行(--noconsole 打包 / pythonw 启动)时看不到
CMD 黑窗口,所有 print 与 logging 输出都无处可看。本模块把日志接到界面:
- 环形缓冲保留最近 N 条,新前端连接时可通过 REST 拉取历史
- 每条新日志经 WS 事件 `log` 实时推送给已连接的界面

注意:logging 可能由任意工作线程触发,而 hub.emit 是协程,必须经
run_coroutine_threadsafe 投递到主事件循环,不能直接在线程里 await。
"""
import asyncio
import logging
import threading
from collections import deque
from datetime import datetime

# 环形缓冲容量:约覆盖数千条日志,足够回溯几十分钟的运行情况
MAX_LINES = 2000


class MemoryLogHandler(logging.Handler):
    """收集日志到内存环形缓冲,并异步广播给前端。"""

    def __init__(self, capacity: int = MAX_LINES):
        super().__init__(level=logging.INFO)
        self.buffer: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._loop = None
        self._emit_fn = None
        self.setFormatter(logging.Formatter("%(message)s"))

    # ---- 生命周期 ----
    def attach(self, loop, emit_fn):
        """绑定事件循环与广播函数(服务启动后调用)。"""
        self._loop = loop
        self._emit_fn = emit_fn

    # ---- logging.Handler ----
    def emit(self, record: logging.LogRecord):
        # 本方法内禁止再打日志,否则会递归触发自己
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            }
        except Exception:
            return
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.buffer.append(entry)
        loop, emit_fn = self._loop, self._emit_fn
        if loop is not None and emit_fn is not None:
            try:
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(emit_fn("log", entry), loop)
            except Exception:
                pass

    # ---- 对外查询 ----
    def history(self, after: int = 0):
        """取 seq > after 的历史日志(新连接用 0 拉全量)。"""
        with self._lock:
            return [e for e in self.buffer if e["seq"] > after]

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def clear(self):
        with self._lock:
            self.buffer.clear()


def install(loop=None, emit_fn=None) -> MemoryLogHandler:
    """挂到 root logger,捕获 uvicorn / sphgj 等全部日志。返回实例。"""
    h = MemoryLogHandler()
    root = logging.getLogger()
    root.addHandler(h)
    # root 默认级别 WARNING,调到 INFO 才能收集到业务日志
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if loop is not None and emit_fn is not None:
        h.attach(loop, emit_fn)
    return h
