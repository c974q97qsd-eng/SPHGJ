import { useCallback, useEffect, useState } from "react"
import { api, type LiveScreenItem, type MetricDef } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"

/** 直播大屏:各账号直播快照 + WS 实时更新 + 卡片指标配置/字典。 */
export function useLiveScreen() {
  const [items, setItems] = useState<LiveScreenItem[]>([])
  const [cardFields, setCardFields] = useState<string[]>([])
  const [metricDict, setMetricDict] = useState<MetricDef[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const [st, dict] = await Promise.all([api.getLiveScreenStatus(), api.getMetricsDictionary()])
      setItems(st.items)
      setCardFields(st.card_fields)
      setMetricDict(dict.metrics)
    } catch { /* ignore */ }
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

  return { items, cardFields, metricDict, loading, refresh }
}
