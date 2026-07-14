import { useCallback, useEffect, useState } from "react"
import { api, type LiveScreenItem } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"

/** 直播大屏:各账号直播快照 + WS 实时更新。 */
export function useLiveScreen() {
  const [items, setItems] = useState<LiveScreenItem[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try { setItems((await api.getLiveScreenStatus()).items) } catch { /* ignore */ }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  useWebSocket(useCallback((e: WsEvent) => {
    if (e.event === "live_screen_update") {
      const p = e.payload as LiveScreenItem
      setItems((prev) => {
        const i = prev.findIndex((x) => x.account_id === p.account_id)
        if (i === -1) return [...prev, p]
        const next = [...prev]
        next[i] = { ...next[i], ...p }
        return next
      })
    }
  }, []))

  return { items, loading, refresh }
}
