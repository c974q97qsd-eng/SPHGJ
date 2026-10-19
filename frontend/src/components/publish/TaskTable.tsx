import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { LoadingState, ErrorState, EmptyState } from "@/components/common/states"
import { api, type PublishTask, type PublishTaskStatus } from "@/lib/api"
import { toast } from "sonner"
import { Send, SkipForward, RotateCcw, Trash2, Pencil, Check, X, Inbox, History } from "lucide-react"

const STATUS_META: Record<PublishTaskStatus, { label: string; variant: "secondary" | "success" | "destructive" | "warning" | "default" }> = {
  pending: { label: "等待发布", variant: "secondary" },
  running: { label: "发布中", variant: "default" },
  success: { label: "已发布", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  skipped: { label: "已跳过", variant: "secondary" },
  need_review: { label: "待核对", variant: "warning" },
}

const STEP_LABEL: Record<string, string> = {
  prepare: "准备", open: "打开页面", upload: "上传", edit: "等转码",
  fill: "填描述", submit: "点发表", verify: "回读校验", done: "完成",
}

function fmtSize(n: number) {
  if (!n) return "-"
  const mb = n / 1024 / 1024
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`
}

function fmtTime(s: string | null) {
  if (!s) return "-"
  return s.replace("T", " ").slice(5, 16)
}

/** 发布任务表。「待发布」与「发布记录」共用,只是筛的状态不同。 */
export function TaskTable({ mode }: { mode: "queue" | "history" }) {
  const [rows, setRows] = useState<PublishTask[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [editId, setEditId] = useState<number | null>(null)
  const [editText, setEditText] = useState("")

  const statusParam = mode === "queue" ? "pending,running,need_review" : "success,failed,skipped"

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const r = await api.publishTasks({ status: statusParam, limit: 200 })
      setRows(r.items)
      setError(null)
    } catch (e) {
      if (!silent) setError((e as Error).message)
    } finally {
      if (!silent) setLoading(false)
    }
  }, [statusParam])

  useEffect(() => { load() }, [load])
  // 发布是长任务(上传/转码),用轮询看进度
  useEffect(() => {
    const t = setInterval(() => load(true), 6000)
    return () => clearInterval(t)
  }, [load])

  const act = async (id: number, fn: () => Promise<unknown>, okMsg: string) => {
    setBusyId(id)
    try { await fn(); toast.success(okMsg); await load(true) }
    catch (e) { toast.error((e as Error).message) }
    finally { setBusyId(null) }
  }

  if (loading) return <LoadingState />
  if (error) return <ErrorState message={error} onRetry={() => load()} />

  if (!rows.length) {
    return (
      <EmptyState
        icon={mode === "queue" ? <Inbox className="h-5 w-5" /> : <History className="h-5 w-5" />}
        title={mode === "queue" ? "没有待发布的作品" : "还没有发布记录"}
        description={mode === "queue"
          ? "把视频丢进「视频号」子目录,系统会自动认领归属并从标题池取一条标题"
          : "发布成功或失败后会在这里留档,可核对错误原因"}
      />
    )
  }

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left font-medium">文件</th>
            <th className="px-3 py-2 text-left font-medium">视频号</th>
            <th className="px-3 py-2 text-left font-medium">标题(描述)</th>
            <th className="px-3 py-2 text-left font-medium">状态</th>
            <th className="px-3 py-2 text-left font-medium">{mode === "queue" ? "入队时间" : "完成时间"}</th>
            <th className="px-3 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const meta = STATUS_META[r.status] || STATUS_META.pending
            const running = r.status === "running"
            return (
              <tr key={r.id} className="border-t align-top hover:bg-muted/30">
                <td className="px-3 py-2">
                  <p className="max-w-[16rem] truncate font-medium" title={r.file_name}>{r.file_name}</p>
                  <p className="text-xs text-muted-foreground">{fmtSize(r.file_size)}</p>
                </td>
                <td className="px-3 py-2 whitespace-nowrap">{r.finder_name || r.finder_id.slice(-8)}</td>
                <td className="px-3 py-2">
                  {editId === r.id ? (
                    <div className="flex items-center gap-1">
                      <Input value={editText} onChange={(e) => setEditText(e.target.value)} className="h-7 w-64 text-xs" autoFocus />
                      <Button size="sm" variant="ghost" className="h-7 px-1"
                        onClick={async () => {
                          try { await api.publishSetTitle(r.id, editText); toast.success("标题已改"); setEditId(null); await load(true) }
                          catch (e) { toast.error((e as Error).message) }
                        }}><Check className="h-3.5 w-3.5" /></Button>
                      <Button size="sm" variant="ghost" className="h-7 px-1" onClick={() => setEditId(null)}><X className="h-3.5 w-3.5" /></Button>
                    </div>
                  ) : (
                    <div className="flex items-start gap-1">
                      <span className="max-w-[22rem] break-words">
                        {r.title || <span className="text-muted-foreground">（无标题,标题池空了）</span>}
                      </span>
                      {!running && (
                        <Button size="sm" variant="ghost" className="h-6 px-1" title="改标题"
                          onClick={() => { setEditId(r.id); setEditText(r.title) }}><Pencil className="h-3 w-3" /></Button>
                      )}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <Badge variant={meta.variant} className="font-normal">{meta.label}</Badge>
                  {running && <p className="mt-1 text-xs text-muted-foreground">{STEP_LABEL[r.step] || r.step || "…"}</p>}
                  {r.status === "success" && r.object_id && (
                    <p className="mt-1 text-xs text-muted-foreground">id {r.object_id.slice(-8)}</p>
                  )}
                  {r.last_error && r.status !== "success" && (
                    <p className="mt-1 max-w-[20rem] text-xs text-destructive/90">{r.last_error}</p>
                  )}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-xs text-muted-foreground">
                  {fmtTime(mode === "queue" ? r.queued_at : (r.finished_at || r.started_at))}
                  {r.attempts > 1 && <span className="ml-1">(第{r.attempts}次)</span>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end gap-1">
                    {mode === "queue" && !running && (
                      <>
                        <Button size="sm" variant="default" className="h-7 gap-1 px-2 text-xs"
                          disabled={busyId === r.id}
                          onClick={() => act(r.id, () => api.publishRun(r.id), "已开始发布")}>
                          <Send className="h-3 w-3" />立即发
                        </Button>
                        <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-xs"
                          disabled={busyId === r.id}
                          onClick={() => act(r.id, () => api.publishSkip(r.id), "已跳过")}>
                          <SkipForward className="h-3 w-3" />跳过
                        </Button>
                      </>
                    )}
                    {mode === "history" && r.status !== "success" && (
                      <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-xs"
                        disabled={busyId === r.id}
                        onClick={() => act(r.id, () => api.publishRetry(r.id), "已放回队列")}>
                        <RotateCcw className="h-3 w-3" />重试
                      </Button>
                    )}
                    {!running && (
                      <Button size="sm" variant="ghost" className="h-7 px-1" title="删除记录"
                        onClick={() => act(r.id, () => api.publishDeleteTask(r.id), "已删除")}>
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
