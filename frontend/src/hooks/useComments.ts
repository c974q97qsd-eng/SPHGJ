import { useCallback, useEffect, useRef, useState } from "react"
import { api, type Comment, type CommentPage } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"

/** 评论列表:分页查询 + WS 实时新增。 */
export function useComments(params: { account_id?: string; replied?: boolean; q?: string; limit?: number; offset?: number }) {
  const [page, setPage] = useState<CommentPage>({ items: [], total: 0, limit: params.limit ?? 200, offset: 0 })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const paramsRef = useRef(params)
  paramsRef.current = params

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const p = await api.getComments(paramsRef.current)
      setPage(p)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh, JSON.stringify(params)])

  // WS:新评论 -> 前置插入;回复/删除 -> 局部更新
  useWebSocket(useCallback((e: WsEvent) => {
    if (e.event === "comments_update") {
      const incoming = (e.payload as { comments: Comment[] }).comments
      setPage((prev) => ({
        ...prev,
        items: [...incoming, ...prev.items].slice(0, prev.limit),
        total: prev.total + incoming.length,
      }))
    } else if (e.event === "comment_replied") {
      const cid = (e.payload as { comment_id: string }).comment_id
      setPage((prev) => ({ ...prev, items: prev.items.map((c) => c.comment_id === cid ? { ...c, replied: true } : c) }))
    } else if (e.event === "comment_deleted") {
      const cid = (e.payload as { comment_id: string }).comment_id
      setPage((prev) => ({ ...prev, items: prev.items.filter((c) => c.comment_id !== cid), total: Math.max(0, prev.total - 1) }))
    }
  }, []))

  return { page, items: page.items, total: page.total, loading, error, refresh }
}
