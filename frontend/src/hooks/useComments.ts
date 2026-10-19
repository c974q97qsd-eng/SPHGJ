import { useCallback, useEffect, useRef, useState } from "react"
import { api, type Comment, type CommentPage } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"

/** 评论列表:分页查询 + WS 实时新增。
 *
 * 分页:limit / offset 由调用方给(以前 fixed 200~300 条一次渲染,滚动和勾选都卡)。
 * WS 新评论的插入策略:只有停在【第一页】时才前置插入 —— 否则会打乱当前页的
 * 分页窗口(第 N 页看到的是别人的内容),此时只更新 total,翻回第一页即可看到。
 */
export function useComments(params: { finder_id?: string; account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number }) {
  const [page, setPage] = useState<CommentPage>({ items: [], total: 0, limit: params.limit ?? 50, offset: params.offset ?? 0 })
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
      const all = (e.payload as { comments: Comment[] }).comments
      if (!all?.length) return
      // 正在筛选某个视频号/账号时,只收归属匹配的新评论 —— 否则别的视频号的新评论
      // 会被插进当前筛选结果里(列表串号)
      const cur = paramsRef.current
      const incoming = all.filter((c) =>
        (!cur.finder_id || c.finder_id === cur.finder_id) &&
        (!cur.account_id || c.account_id === cur.account_id))
      if (!incoming.length) return
      const onFirstPage = (paramsRef.current.offset ?? 0) === 0
      setPage((prev) => ({
        ...prev,
        items: onFirstPage
          ? [...incoming, ...prev.items].slice(0, prev.limit)
          : prev.items,
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

  return { page, items: page.items, total: page.total, limit: page.limit,
           offset: page.offset, loading, error, refresh }
}
