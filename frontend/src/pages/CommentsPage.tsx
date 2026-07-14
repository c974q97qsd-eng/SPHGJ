import { useEffect, useMemo, useState } from "react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription, SheetFooter } from "@/components/ui/sheet"
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table"
import { Checkbox } from "@/components/ui/checkbox"
import { EmptyState, LoadingState, ErrorState } from "@/components/common/states"
import { useComments } from "@/hooks/useComments"
import { useAccounts } from "@/hooks/useAccounts"
import { api, type Comment } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { toast } from "sonner"
import { Search, Download, Send, Trash2, Pin, MessageSquare, Reply as ReplyIcon, AlertTriangle, Loader2 } from "lucide-react"

type RepliedFilter = "all" | "unreplied" | "replied"

export function CommentsPage() {
  const { accounts } = useAccounts()
  const [accountId, setAccountId] = useState<string>("all")
  const [q, setQ] = useState("")
  const [replied, setReplied] = useState<RepliedFilter>("all")
  const [selected, setSelected] = useState<Comment | null>(null)

  const params = useMemo(() => ({
    account_id: accountId === "all" ? undefined : accountId,
    replied: replied === "all" ? undefined : replied === "replied",
    q: q.trim() || undefined,
    limit: 300,
  }), [accountId, q, replied])

  const { items, total, loading, error, refresh } = useComments(params)

  // 新评论高亮(由 useComments 前置插入,这里取最新一条做短暂高亮)
  const accMap = useMemo(() => Object.fromEntries(accounts.map((a) => [a.id, a])), [accounts])

  // 批量勾选
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set())
  const [batchDeleting, setBatchDeleting] = useState(false)
  const allChecked = items.length > 0 && items.every((c) => checkedIds.has(c.comment_id))
  const toggleAll = () => setCheckedIds(allChecked ? new Set() : new Set(items.map((c) => c.comment_id)))
  const toggleOne = (id: string) => setCheckedIds((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })
  // 筛选变化清空勾选
  useEffect(() => { setCheckedIds(new Set()) }, [accountId, q, replied])

  const batchDelete = async () => {
    const targets = items.filter((c) => checkedIds.has(c.comment_id))
    if (!targets.length) return
    if (!window.confirm(`确认删除选中的 ${targets.length} 条评论?此操作不可撤销。`)) return
    setBatchDeleting(true)
    try {
      const r = await api.batchDeleteComments(targets.map((c) => ({ comment_id: c.comment_id, account_id: c.account_id, export_id: c.export_id })))
      toast.success(`已删除 ${r.deleted} 条` + (r.failed.length ? `,失败 ${r.failed.length} 条` : ""))
      setCheckedIds(new Set())
      refresh()
    } catch (e) {
      toast.error("批量删除失败:" + (e as Error).message)
    } finally {
      setBatchDeleting(false)
    }
  }

  const [deletingId, setDeletingId] = useState<string | null>(null)
  const handleDelete = async (c: Comment) => {
    if (!window.confirm("确认删除该评论?")) return
    setDeletingId(c.comment_id)
    try {
      await api.deleteComment(c.comment_id, c.account_id, c.export_id)
      toast.success("已删除")
      // WS comment_deleted 会自动从列表移除
    } catch (e) {
      toast.error("删除失败:" + (e as Error).message)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <div className="space-y-4">
      {/* 筛选栏 */}
      <Card className="p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={accountId} onValueChange={setAccountId}>
            <SelectTrigger className="w-[160px]"><SelectValue placeholder="全部账号" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部账号</SelectItem>
              {accounts.map((a) => <SelectItem key={a.id} value={a.id}>{a.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={replied} onValueChange={(v) => setReplied(v as RepliedFilter)}>
            <SelectTrigger className="w-[120px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="unreplied">未回复</SelectItem>
              <SelectItem value="replied">已回复</SelectItem>
            </SelectContent>
          </Select>
          <div className="relative flex-1 min-w-[180px]">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input className="pl-8" placeholder="搜索评论内容或用户…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <Button variant="outline" size="sm" asChild className="gap-1.5">
            <a href={api.exportCsvUrl(accountId === "all" ? undefined : accountId)}><Download className="h-3.5 w-3.5" />导出 CSV</a>
          </Button>
        </div>
      </Card>

      <div className="flex items-center justify-between px-1">
        <p className="text-sm text-muted-foreground">共 {total} 条评论{checkedIds.size > 0 && ` · 已选 ${checkedIds.size} 条`}</p>
        <div className="flex items-center gap-3">
          <p className="hidden sm:block text-xs text-muted-foreground">点击评论行进行手动回复 / 删除</p>
          {checkedIds.size > 0 && (
            <Button variant="destructive" size="sm" onClick={batchDelete} disabled={batchDeleting} className="gap-1.5">
              {batchDeleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
              批量删除({checkedIds.size})
            </Button>
          )}
        </div>
      </div>

      {/* 评论表 */}
      <Card>
        {error ? <ErrorState message={error} onRetry={refresh} />
          : loading && items.length === 0 ? <LoadingState />
          : items.length === 0 ? (
            <EmptyState icon={<MessageSquare className="h-6 w-6" />} title="暂无评论"
              description="启动引擎抓取评论后,这里会实时显示。新评论到达会自动出现在列表顶部。" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[40px]">
                    <Checkbox checked={allChecked} onChange={toggleAll} aria-label="全选当前页" disabled={items.length === 0} />
                  </TableHead>
                  <TableHead className="w-[100px]">账号</TableHead>
                  <TableHead className="w-[110px]">用户</TableHead>
                  <TableHead>评论内容</TableHead>
                  <TableHead className="w-[120px]">时间</TableHead>
                  <TableHead className="w-[60px]">赞</TableHead>
                  <TableHead className="w-[70px]">状态</TableHead>
                  <TableHead className="w-[60px] text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((c) => (
                  <TableRow
                    key={c.comment_id}
                    data-state={selected?.comment_id === c.comment_id ? "selected" : undefined}
                    onClick={() => setSelected(c)}
                    className="cursor-pointer"
                  >
                    <TableCell className="w-[40px]" onClick={(e) => e.stopPropagation()}>
                      <Checkbox
                        checked={checkedIds.has(c.comment_id)}
                        onChange={() => toggleOne(c.comment_id)}
                        aria-label="选择该评论"
                      />
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">{accMap[c.account_id]?.name || c.account_id}</TableCell>
                    <TableCell className="max-w-[110px] truncate text-xs">{c.nickname}</TableCell>
                    <TableCell className="max-w-[420px] truncate">{c.content}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{fmtTime(c.create_time)}</TableCell>
                    <TableCell className="text-xs">{c.like_count || 0}</TableCell>
                    <TableCell>{c.replied ? <Badge variant="success" className="font-normal">已回</Badge> : <Badge variant="secondary" className="font-normal">未回</Badge>}</TableCell>
                    <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
                      <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground hover:text-destructive" disabled={!accMap[c.account_id]?.logged_in || deletingId === c.comment_id} onClick={() => handleDelete(c)} aria-label="删除评论">
                        {deletingId === c.comment_id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
      </Card>

      {/* 上下文手动回复抽屉(诉求3:仅选中时触发) */}
      <ReplySheet comment={selected} accountName={selected ? accMap[selected.account_id]?.name : undefined}
        loggedIn={selected ? accMap[selected.account_id]?.logged_in : false}
        onClose={() => setSelected(null)} onDone={refresh} />
    </div>
  )
}

function ReplySheet({ comment, accountName, loggedIn, onClose, onDone }: {
  comment: Comment | null
  accountName?: string
  loggedIn?: boolean
  onClose: () => void
  onDone: () => void
}) {
  const [text, setText] = useState("")
  const [acting, setActing] = useState(false)

  // 切换选中评论时重置输入
  useEffect(() => { setText("") }, [comment?.comment_id])

  if (!comment) return null
  const cantAct = !loggedIn

  const reply = async () => {
    if (!text.trim()) { toast.error("请输入回复内容"); return }
    setActing(true)
    try { await api.replyComment(comment.comment_id, comment.account_id, text); toast.success("回复已发送"); setText(""); onDone() }
    catch (e) { toast.error("回复失败:" + (e as Error).message) }
    finally { setActing(false) }
  }
  const del = async () => {
    setActing(true)
    try { await api.deleteComment(comment.comment_id, comment.account_id, comment.export_id); toast.success("已删除"); onClose(); onDone() }
    catch (e) { toast.error("删除失败:" + (e as Error).message) }
    finally { setActing(false) }
  }
  const pin = async () => {
    setActing(true)
    try { await api.pinComment(comment.comment_id, comment.account_id, comment.export_id); toast.success("已置顶") }
    catch (e) { toast.error("置顶失败:" + (e as Error).message) }
    finally { setActing(false) }
  }

  return (
    <Sheet open={!!comment} onOpenChange={(v) => !v && onClose()}>
      <SheetContent className="flex flex-col gap-0">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2"><ReplyIcon className="h-4 w-4 text-primary" />手动回复</SheetTitle>
          <SheetDescription>回复选中评论 · 账号 {accountName || comment.account_id}</SheetDescription>
        </SheetHeader>

        {/* 原评论 */}
        <div className="px-6 pb-2">
          <div className="rounded-lg bg-muted/50 p-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-sm font-medium">{comment.nickname}</span>
              <span className="text-xs text-muted-foreground">{fmtTime(comment.create_time)}</span>
            </div>
            <p className="text-sm break-words">{comment.content}</p>
          </div>
        </div>

        {cantAct && (
          <div className="mx-6 mb-2 flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <AlertTriangle className="h-3.5 w-3.5" />该账号未启动或未登录,请先在「账号管理」启动
          </div>
        )}

        {/* 回复输入 */}
        <div className="flex-1 px-6 pb-2">
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="输入回复内容…(Enter 发送)"
            className="h-full min-h-[120px]"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); reply() } }}
            autoFocus
            disabled={cantAct}
          />
        </div>

        <SheetFooter className="flex-row justify-between gap-2 border-t p-4 pt-3">
          <Button variant="ghost" onClick={onClose}>关闭</Button>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={pin} disabled={acting || cantAct} className="gap-1.5"><Pin className="h-3.5 w-3.5" />置顶</Button>
            <Button variant="destructive" size="sm" onClick={del} disabled={acting || cantAct} className="gap-1.5">{acting ? <Loader2 className="animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}删除</Button>
            <Button size="sm" onClick={reply} disabled={acting || cantAct} className="gap-1.5">{acting ? <Loader2 className="animate-spin" /> : <Send className="h-3.5 w-3.5" />}回复</Button>
          </div>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
