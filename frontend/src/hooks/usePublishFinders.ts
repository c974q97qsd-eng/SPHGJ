import { useCallback, useEffect, useRef, useState } from "react"
import { api, type PublishFinder } from "@/lib/api"

/**
 * 可发布视频号列表(带各状态任务条数与标题余量)。
 *
 * 发布管理三个页签都要用同一份数据画「账号选项卡片」,抽成 hook 避免三处各写一遍
 * 拉取+轮询。拿不到就保持上一份,不弹错——卡片只是筛选辅助,不能因为它挂掉整页。
 */
export function usePublishFinders(pollSec = 15) {
  const [finders, setFinders] = useState<PublishFinder[]>([])
  const timer = useRef<ReturnType<typeof setInterval>>()

  const load = useCallback(async () => {
    try {
      const r = await api.publishFinders()
      setFinders(r.items)
    } catch {
      /* 静默:筛选卡片失败不影响主列表 */
    }
  }, [])

  useEffect(() => {
    load()
    if (pollSec > 0) {
      timer.current = setInterval(load, pollSec * 1000)
      return () => clearInterval(timer.current)
    }
  }, [load, pollSec])

  return { finders, reload: load }
}

/** 待发布口径:排队 + 发布中 + 待核对,三者在「待发布」页签里都会显示 */
export function pendingCountOf(f: PublishFinder) {
  const c = f.counts || {}
  return (c.pending || 0) + (c.running || 0) + (c.need_review || 0)
}

/** 发布记录口径:已发布 + 失败 + 已跳过 */
export function historyCountOf(f: PublishFinder) {
  const c = f.counts || {}
  return (c.success || 0) + (c.failed || 0) + (c.skipped || 0)
}
