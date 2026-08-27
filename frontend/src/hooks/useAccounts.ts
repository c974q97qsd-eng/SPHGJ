import { useCallback, useEffect, useRef, useState } from "react"
import { api, type AccountStatus, type EngineStatus } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"

/** 账号列表 + 引擎状态:轮询 + WS 实时。 */
export function useAccounts() {
  const [status, setStatus] = useState<EngineStatus>({ running: false, accounts: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setInterval>>()

  const refresh = useCallback(async () => {
    try {
      const s = await api.getAccounts()
      setStatus(s)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    timer.current = setInterval(refresh, 4000)
    return () => clearInterval(timer.current)
  }, [refresh])

  // WS:引擎状态变化立即更新
  useWebSocket(useCallback((e: WsEvent) => {
    if (e.event === "engine_status") setStatus(e.payload as EngineStatus)
  }, []))

  return { status, accounts: status.accounts, loading, error, refresh }
}

/** 单账号操作。 */
export function useAccountActions(refresh: () => void) {
  const [busy, setBusy] = useState<string | null>(null)
  const act = useCallback(async <T,>(id: string, fn: (id: string) => Promise<T>): Promise<T> => {
    setBusy(id)
    try { return await fn(id) } finally { setBusy(null); refresh() }
  }, [refresh])
  return {
    busy,
    start: (id: string) => act(id, api.startAccount),
    stop: (id: string) => act(id, api.stopAccount),
    openBrowser: (id: string) => act(id, api.openAccountBrowser),
    openDashboard: (id: string) => act(id, api.openDashboard),
    remove: (id: string, removeProfile: boolean) => act(id, (i) => api.deleteAccount(i, removeProfile)),
  }
}

export type { AccountStatus }
