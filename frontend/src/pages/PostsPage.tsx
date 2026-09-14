import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Switch } from "@/components/ui/switch"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { EmptyState, LoadingState, ErrorState } from "@/components/common/states"
import { useAccounts } from "@/hooks/useAccounts"
import { api, type PostItem, type PostsJobState } from "@/lib/api"
import { fmtTime, cn } from "@/lib/utils"
import { toast } from "sonner"
import {
  Search, RefreshCw, Loader2, EyeOff, Eye, Pin, PinOff,
  ChevronLeft, ChevronRight, ArrowUpDown, Clapperboard,
} from "lucide-react"

type VisibleFilter = "all" | "public" | "hidden" | "sticky"
type SortKey = "create_time" | "read_count" | "like_count" | "comment_count" | "forward_count" | "fav_count"

const SORT_LABEL: Record<SortKey, string> = {
  create_time: "发布时间", read_count: "播放量", like_count: "点赞",
  comment_count: "评论数", forward_count: "转发", fav_count: "收藏",
}

function fmtCount(n: number | null | undefined): string {
  const v = n || 0
  if (v >= 100000000) return (v / 100000000).toFixed(1) + "亿"
  if (v >= 10000) return (v / 10000).toFixed(1) + "w"
  return String(v)
}

export function PostsPage() {
  const { accounts } = useAccounts()
  const [accountId, setAccountId] = useState<string>("all")
  const [visible, setVisible] = useState<VisibleFilter>("all")
  const [sort, setSort] = useState<SortKey>("create_time")
  const [asc, setAsc] = useState(false)
  const [dateFrom, setDateFrom] = useState("")
  const [dateTo, setDateTo] = useState("")
  const [q, setQ] = useState("")
  const [searchQ, setSearchQ] = useState("") // 回车才触发搜索,避免每键一次请求
  const [offset, setOffset] = useState(0)
  const limit = 50

  const [items, setItems] = useState<PostItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [job, setJob] = useState<PostsJobState | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [batching, setBatching] = useState(false)
  const [meta, setMeta] = useState<Record<string, { last_refresh: string | null; last_pages: number; today_requests: number }>>({})

  // 自动刷新配置
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [autoHour, setAutoHour] = useState("9")
  const autoLoaded = useRef(false)

  const [checked, setChecked] = useState<Set<string>>(new Set())
  const allChecked = items.length > 0 && items.every((p) => checked.has(p.object_id))
  const toggleAll = () => setChecked(allChecked ? new Set() : new Set(items.map((p) => p.object_id)))
  const toggleOne = (oid: string) => setChecked((prev) => {
    const next = new Set(prev)
    if (next.has(oid)) next.delete(oid); else next.add(oid)
    return next
  })

  const params = useMemo(() => ({
    account_id: accountId === "all" ? undefined : accountId,
    visible: visible === "all" ? undefined : visible,
    sort, order: asc ? "asc" : "desc",
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
    q: searchQ.trim() || undefined,
    limit, offset,
  }), [accountId, visible, sort, asc, dateFrom, dateTo, searchQ, offset])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const p = await api.getPosts(params)
      setItems(p.items)
      setTotal(p.total)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [params])

  const loadMeta = useCallback(async () => {
    try { setMeta((await api.getPostsMeta()).meta) } catch { /* ignore */ }
  }, [])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => { loadMeta() }, [loadMeta])
  useEffect(() => { setChecked(new Set()) }, [accountId, visible, sort, asc, dateFrom, dateTo, searchQ])

  // 自动刷新配置加载
  useEffect(() => {
    api.getConfig().then((c) => {
      if (autoLoaded.current) return
      autoLoaded.current = true
      setAutoRefresh(c.posts?.auto_refresh_enabled ?? true)
      setAutoHour(String(c.posts?.auto_refresh_hour ?? 9))
    }).catch(() => {})
  }, [])

  const saveAuto = async (enabled: boolean, hour: string) => {
    try {
      await api.patchConfig({ posts_auto_refresh_enabled: enabled, posts_auto_refresh_hour: parseInt(hour, 10) || 0 })
      toast.success("自动刷新设置已保存")
    } catch (e) { toast.error((e as Error).message) }
  }

  // 任务进度轮询(刷新/批量共用)
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const j = await api.postsJob()
        setJob((prev) => {
          if (prev?.running && !j.running) {
            // 刚结束:提示 + 刷新列表
            const failN = j.failed?.length || 0
            if (j.kind === "refresh") {
              const oks = Object.values(j.accounts || {}).filter((a: Record<string, unknown>) => a.status === "ok").length
              toast.success(`作品刷新完成(${oks} 个账号)` + (failN ? `,异常 ${failN}` : ""))
            } else {
              toast.success(`批量操作完成(${j.done}/${j.total})` + (failN ? `,失败 ${failN}` : ""))
            }
            refresh()
            loadMeta()
            setRefreshing(false)
            setBatching(false)
            setChecked(new Set())
          }
          return j
        })
      } catch { /* ignore */ }
    }, 1500)
    return () => clearInterval(t)
  }, [refresh, loadMeta])

  const startRefresh = async () => {
    setRefreshing(true)
    try {
      await api.postsRefresh(accountId === "all" ? undefined : accountId)
      toast.info("作品刷新已开始,进度见下方提示")
    } catch (e) {
      toast.error((e as Error).message)
      setRefreshing(false)
    }
  }

  const doBatch = async (action: "hide" | "unhide" | "sticky" | "unsticky") => {
    const targets = items.filter((p) => checked.has(p.object_id))
    if (!targets.length) return
    const label = { hide: "隐藏(仅自己可见)", unhide: "取消隐藏(公开)", sticky: "置顶", unsticky: "取消置顶" }[action]
    if (!window.confirm(`确认对选中的 ${targets.length} 个作品执行「${label}」?`)) return
    setBatching(true)
    try {
      await api.postsBatch(action, targets.map((p) => ({ account_id: p.account_id, object_id: p.object_id })))
      toast.info("批量操作已开始(写操作逐条节流,防风控),进度见下方提示")
    } catch (e) {
      toast.error((e as Error).message)
      setBatching(false)
    }
  }

  const toggleSortDir = () => { setAsc((v) => !v); setOffset(0) }

  const accName = (id: string) => accounts.find((a) => a.id === id)?.name || id
  const totalToday = Object.values(meta).reduce((s, m) => s + (m.today_requests || 0), 0)
  const lastRefresh = Object.values(meta)
    .map((m) => m.last_refresh).filter(Boolean).sort().reverse()[0]

  return (
    <div className="space-y-4">
      {/* 筛选栏 */}
      <Card className="p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={accountId} onValueChange={(v) => { setAccountId(v); setOffset(0) }}>
            <SelectTrigger className="w-[160px]"><SelectValue placeholder="全部账号" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部账号</SelectItem>
              {accounts.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={visible} onValueChange={(v) => { setVisible(v as VisibleFilter); setOffset(0) }}>
            <SelectTrigger className="w-[120px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部状态</SelectItem>
              <SelectItem value="public">公开</SelectItem>
              <SelectItem value="hidden">已隐藏</SelectItem>
              <SelectItem value="sticky">已置顶</SelectItem>
            </SelectContent>
          </Select>
          <Select value={sort} onValueChange={(v) => { setSort(v as SortKey); setOffset(0) }}>
            <SelectTrigger className="w-[110px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => (
                <SelectItem key={k} value={k}>按{SORT_LABEL[k]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="outline" size="icon" className="h-9 w-9" onClick={toggleSortDir} aria-label="切换升降序">
            <ArrowUpDown className={cn("h-3.5 w-3.5", asc && "rotate-180")} />
          </Button>
          <div className="flex items-center gap-1">
            <Input type="date" className="w-[130px] h-9 text-xs" value={dateFrom}
              onChange={(e) => { setDateFrom(e.target.value); setOffset(0) }} aria-label="发布起始日期" />
            <span className="text-xs text-muted-foreground">至</span>
            <Input type="date" className="w-[130px] h-9 text-xs" value={dateTo}
              onChange={(e) => { setDateTo(e.target.value); setOffset(0) }} aria-label="发布截止日期" />
          </div>
          <div className="relative flex-1 min-w-[160px]">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-8" placeholder="按标题搜索,回车确认…" value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { setSearchQ(q); setOffset(0) } }} />
          </div>
          <Button size="sm" onClick={startRefresh} disabled={refreshing || job?.running} className="gap-1.5">
            {refreshing || job?.running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            刷新作品
          </Button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
          <span>共 {total} 个作品{checked.size > 0 && ` · 已选 ${checked.size} 个`}</span>
          {lastRefresh && <span>上次更新:{new Date(lastRefresh).toLocaleString()}</span>}
          <span>今日已请求 {totalToday} 次</span>
          <div className="flex items-center gap-2 ml-auto">
            <span>每日自动刷新({autoHour}:00 在线账号)</span>
            <Switch checked={autoRefresh} onCheckedChange={(v) => { setAutoRefresh(v); saveAuto(v, autoHour) }} aria-label="每日自动刷新" />
            <Select value={autoHour} onValueChange={(v) => { setAutoHour(v); saveAuto(autoRefresh, v) }}>
              <SelectTrigger className="h-7 w-[64px] text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                {Array.from({ length: 24 }, (_, h) => (
                  <SelectItem key={h} value={String(h)}>{String(h).padStart(2, "0")}:00</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      </Card>

      {/* 批量操作栏 */}
      {checked.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border bg-card p-2.5">
          <span className="text-sm text-muted-foreground mr-1">已选 {checked.size} 个:</span>
          <Button variant="secondary" size="sm" onClick={() => doBatch("hide")} disabled={batching || job?.running} className="gap-1.5">
            <EyeOff className="h-3.5 w-3.5" />隐藏
          </Button>
          <Button variant="outline" size="sm" onClick={() => doBatch("unhide")} disabled={batching || job?.running} className="gap-1.5">
            <Eye className="h-3.5 w-3.5" />取消隐藏
          </Button>
          <Button variant="outline" size="sm" onClick={() => doBatch("sticky")} disabled={batching || job?.running} className="gap-1.5">
            <Pin className="h-3.5 w-3.5" />置顶
          </Button>
          <Button variant="outline" size="sm" onClick={() => doBatch("unsticky")} disabled={batching || job?.running} className="gap-1.5">
            <PinOff className="h-3.5 w-3.5" />取消置顶
          </Button>
          {batching && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Loader2 className="h-3 w-3 animate-spin" />执行中(逐条节流)…</span>}
        </div>
      )}

      {/* 任务进度 */}
      {job?.running && (
        <Card className="p-2.5 text-xs text-muted-foreground">
          {job.kind === "refresh" ? "正在刷新作品" : "正在执行批量操作"} · 总 {job.total} · 完成 {job.done}
          {job.accounts && Object.entries(job.accounts).map(([aid, a]) => (
            <span key={aid} className="ml-2">
              {accName(aid)}:{(a as Record<string, unknown>).status as string}
              {typeof (a as Record<string, unknown>).fetched === "number" && `(${(a as Record<string, unknown>).fetched} 条)`}
            </span>
          ))}
        </Card>
      )}

      {/* 作品网格 */}
      <Card>
        {error ? <ErrorState message={error} onRetry={refresh} />
          : loading && items.length === 0 ? <LoadingState />
          : items.length === 0 ? (
            <EmptyState icon={<Clapperboard className="h-6 w-6" />} title="暂无作品"
              description="点右上角「刷新作品」抓取,数据本地化后浏览/搜索/排序全程零官方请求。" />
          ) : (
            <div className="p-3">
              <div className="mb-2 flex items-center gap-2 px-1">
                <Checkbox checked={allChecked} onChange={toggleAll} aria-label="全选本页" />
                <span className="text-xs text-muted-foreground">全选本页({items.length})</span>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
                {items.map((p) => (
                  <div key={p.object_id}
                    className={cn("group relative overflow-hidden rounded-lg border bg-card transition-colors",
                      checked.has(p.object_id) && "border-primary ring-1 ring-primary")}>
                    <div className="relative aspect-video w-full overflow-hidden bg-muted">
                      {p.cover_path ? (
                        <img src={p.cover_path} alt="" loading="lazy"
                          className="h-full w-full object-cover"
                          onError={(e) => { (e.target as HTMLImageElement).style.display = "none" }} />
                      ) : null}
                      <div className="absolute left-1.5 top-1.5">
                        <Checkbox checked={checked.has(p.object_id)} onChange={() => toggleOne(p.object_id)} aria-label="选择该作品" />
                      </div>
                      <div className="absolute right-1.5 top-1.5 flex gap-1">
                        {p.is_sticky && <Badge className="font-normal text-xs px-1.5 py-0">置顶</Badge>}
                        {p.is_hidden && <Badge variant="destructive" className="font-normal text-xs px-1.5 py-0">已隐藏</Badge>}
                      </div>
                      <div className="absolute bottom-0 left-0 right-0 flex gap-3 bg-black/60 px-2 py-1 text-xs text-white">
                        <span>▶ {fmtCount(p.read_count)}</span>
                        <span>👍 {fmtCount(p.like_count)}</span>
                        <span>💬 {fmtCount(p.comment_count)}</span>
                      </div>
                    </div>
                    <div className="p-2">
                      <p className="line-clamp-2 min-h-[2.5rem] text-xs leading-5" title={p.title}>
                        {p.title || <span className="text-muted-foreground">(无标题)</span>}
                      </p>
                      <div className="mt-1 flex items-center justify-between text-xs text-muted-foreground">
                        <span className="truncate max-w-[45%]">{accName(p.account_id)}</span>
                        <span>{p.create_time ? fmtTime(p.create_time) : "-"}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              {/* 分页 */}
              <div className="mt-3 flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  第 {offset + 1}-{Math.min(offset + limit, total)} / 共 {total}
                </span>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - limit))} className="gap-1">
                    <ChevronLeft className="h-3.5 w-3.5" />上一页
                  </Button>
                  <Button variant="outline" size="sm" disabled={offset + limit >= total}
                    onClick={() => setOffset(offset + limit)} className="gap-1">
                    下一页<ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            </div>
          )}
      </Card>
    </div>
  )
}
