import { useEffect, useRef, useState, useCallback } from "react"

/** 后端推送事件类型。 */
export type WsEvent =
  | { event: "qr_update"; payload: { sid: string; image: string } }
  | { event: "login_status"; payload: { sid: string; status: string; captured?: { aid: string; finder_id: string; name: string }; error?: string } }
  | { event: "engine_status"; payload: { running: boolean; accounts: unknown[] } }
  | { event: "comments_update"; payload: { account_id: string; comments: unknown[] } }
  | { event: "comment_replied"; payload: { comment_id: string } }
  | { event: "comment_deleted"; payload: { comment_id: string } }
  | { event: "live_screen_update"; payload: { account_id: string; name: string; live_stats: unknown; stream_url: string | null; updated_at: string; is_live?: boolean; metrics?: Record<string, number | null> } }
  | { event: "relogin_queue"; payload: { active: string | null; active_name?: string; pending: string[] } }

/**
 * WebSocket 订阅。自动重连。返回最近事件 + 订阅回调注册。
 * 全局单例连接:多 hook 共享一条 ws,各自注册 listener。
 */
type Listener = (e: WsEvent) => void
const listeners = new Set<Listener>()
let socket: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null

function ensureSocket() {
  if (socket && socket.readyState <= 1) return
  const proto = location.protocol === "https:" ? "wss" : "ws"
  socket = new WebSocket(`${proto}://${location.host}/ws`)
  socket.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data) as WsEvent
      listeners.forEach((l) => l(data))
    } catch { /* ignore */ }
  }
  socket.onclose = () => {
    socket = null
    if (!reconnectTimer) reconnectTimer = setTimeout(ensureSocket, 2000)
  }
  socket.onerror = () => socket?.close()
}

export function useWebSocket(onEvent?: Listener) {
  const cbRef = useRef(onEvent)
  cbRef.current = onEvent

  useEffect(() => {
    ensureSocket()
    const l: Listener = (e) => cbRef.current?.(e)
    listeners.add(l)
    return () => { listeners.delete(l) }
  }, [])

  return { connected: true }
}

/** 便捷:取最近一次某事件 payload。 */
export function useLatestEvent<T = unknown>(eventName: string): T | null {
  const [latest, setLatest] = useState<T | null>(null)
  useWebSocket(useCallback((e: WsEvent) => {
    if (e.event === eventName) setLatest(e.payload as T)
  }, [eventName]))
  return latest
}
