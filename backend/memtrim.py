"""内存归还(trim):定时 gc.collect() + 平台级堆/工作集归还。

背景(rev15 实测发现):
  Python 的 pymalloc 分配器 freed 后的内存【不归还给 OS】,长跑进程的 RSS 只增不减——
  即使对象已被 GC 回收,堆空洞仍占用虚拟内存。实测 4 账号运行数小时后 Python 主进程
  RSS 顶在 ~3.5GB,而实际活跃对象远小于此:rev13 已消除每秒数千 CDP 事件的 churn,
  RSS 仍高位 -> 主要是【历史分配残留的堆碎片】,而非持续泄漏。

方案(两步缺一不可):
  1) gc.collect()
     回收循环引用(引用计数无法处理的孤岛对象)。Python 的自动 GC 分代触发,
     长跑进程中 gen2 回收周期很长,死对象可能滞留很久。
  2) 平台级归还
     - Windows: SetProcessWorkingSetSize(-1, -1) 请求 OS 把进程物理工作集压缩到最小,
       未使用的物理页被释放 -> 任务管理器里看到的占用【立即下降】。
     - Linux:   malloc_trim(0) 归还 glibc 堆顶空闲内存给 OS。

注意:
  - 这是【内存归还】而非【泄漏修复】。若 trim 后 RSS 不降、且持续增长,才是真泄漏,
    应另查对象持有链(如 Playwright Response/Route 对象累积)。
  - 频率不宜过高:gc.collect() 全量回收有 STW 开销,默认 10 分钟一次足够。
  - 工作集压缩只是"把物理页还给 OS",虚拟地址空间不变;进程再次需要时会重新提交,
    有轻微页错误开销,但远小于常驻 3.5GB 的代价。
"""
import gc
import asyncio
import logging
import platform

logger = logging.getLogger("sphgj")


def _trim_windows():
    """Windows: SetProcessWorkingSetSize(-1,-1) 请求 OS 压缩物理工作集。"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetProcessWorkingSetSize(
            ctypes.windll.kernel32.GetCurrentProcess(), -1, -1)
        return True
    except Exception as e:
        logger.debug(f"[memtrim] SetProcessWorkingSetSize 失败: {e}")
        return False


def _trim_linux():
    """Linux: malloc_trim(0) 归还 glibc 堆顶空闲内存给 OS。"""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
        return True
    except Exception as e:
        logger.debug(f"[memtrim] malloc_trim 失败: {e}")
        return False


def trim_now():
    """执行一次完整内存归还:gc.collect() + 平台相关归还。

    返回 (gc 回收对象数, 平台归还是否成功)。
    """
    collected = gc.collect()
    if platform.system() == "Windows":
        ok = _trim_windows()
    else:
        ok = _trim_linux()
    logger.debug(f"[memtrim] gc 回收 {collected} 个对象,平台归还={'成功' if ok else '失败'}")
    return collected, ok


class MemoryTrimmer:
    """后台定时执行内存归还的任务。"""

    def __init__(self, interval_sec=600):
        self.interval = interval_sec
        self._task = None

    async def _loop(self):
        # 首次延迟一个完整周期:启动阶段对象分配剧烈(浏览器启动/首次抓取),
        # 过早 trim 意义不大且徒增 STW 开销。
        await asyncio.sleep(self.interval)
        while True:
            try:
                # to_thread:gc.collect() + 系统调用是同步阻塞,不能卡住事件循环
                collected, ok = await asyncio.to_thread(trim_now)
                logger.info(
                    f"[memtrim] 定时内存归还完成"
                    f"(gc 回收 {collected} 对象,工作集压缩={'OK' if ok else '跳过'})"
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[memtrim] 定时归还异常: {e}")
            await asyncio.sleep(self.interval)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(f"[memtrim] 启动定时内存归还(每 {self.interval}s)")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
            logger.info("[memtrim] 已停止定时内存归还")
