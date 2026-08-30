/**
 * 运行日志面板(界面底部可折叠)。
 *
 * 背景:软件以无控制台方式运行(打包 exe / pythonw 启动)时没有 CMD 黑窗口,
 * 后端 logging 输出无处可看。本组件从 REST 拉历史 + WS 收实时增量,
 * 把日志搬到界面上。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ChevronDown, ChevronUp, Terminal, Trash2, Play, Pause } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { api } from "@/lib/api"
import { useWebSocket, type LogLine, type WsEvent } from "@/lib/ws"
import { cn } from "@/lib/utils"

const MAX_LINES = 2000

const LEVEL_STYLE: Record<string, string> = {
  DEBUG: "text-muted-foreground/70",
  INFO: "text-foreground/85",
  WARNING: "text-amber-600 dark:text-amber-400",
  ERROR: "text-red-600 dark:text-red-400",
  CRITICAL: "text-red-700 dark:text-red-300 font-semibold",
}

const FILTERS = [
  { value: "ALL", label: "全部" },
  { value: "INFO", label: "INFO+" },
  { value: "WARNING", label: "警告+" },
  { value: "ERROR", label: "仅错误" },
] as const

const MIN_LEVEL: Record<string, number> = { ALL: 0, INFO: 1, WARNING: 2, ERROR: 3 }
const LEVEL_RANK: Record<string, number> = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 3 }

const isErr = (lv: string) => lv === "ERROR" || lv === "CRITICAL"

export function LogPanel() {
  const [lines, setLines] = useState<LogLine[]>([])
  const [open, setOpen] = useState(false)
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState<string>("ALL")
  const [errCount, setErrCount] = useState(0)

  // 已收 seq 集合:REST 历史与 WS 增量会重叠,按 seq 去重
  const seqsRef = useRef<Set<number>>(new Set())
  const lastSeqRef = useRef(0)
  const boxRef = useRef<HTMLDivElement>(null)

  /** 合并一批日志到状态。reset=true 表示这是全新的历史快照,先清空。 */
  const absorb = useCallback((incoming: LogLine[], reset: boolean) => {
    const seqs = reset ? new Set<number>() : new Set(seqsRef.current)
    const added: LogLine[] = []
    let errs = 0
    for (const l of incoming) {
      if (seqs.has(l.seq)) continue
      seqs.add(l.seq)
      added.push(l)
      if (isErr(l.level)) errs += 1
      if (l.seq > lastSeqRef.current) lastSeqRef.current = l.seq
    }
    if (added.length === 0) return
    seqsRef.current = seqs
    setLines((prev) => {
      const next = reset ? added : [...prev, ...added]
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next
    })
    if (reset) setErrCount(errs)
    else if (errs) setErrCount((c) => c + errs)
  }, [])

  // 挂载时拉一次历史,之后靠 WS 增量
  useEffect(() => {
    api.getLogs(0).then((r) => absorb(r.lines, true)).catch(() => {})
  }, [absorb])

  useWebSocket(useCallback((e: WsEvent) => {
    if (e.event === "log") absorb([e.payload], false)
  }, [absorb]))

  // WS 断线期间的日志会漏,定时按 seq 补拉增量
  useEffect(() => {
    const t = setInterval(() => {
      if (document.hidden) return
      api.getLogs(lastSeqRef.current)
        .then((r) => { if (r.lines.length) absorb(r.lines, false) })
        .catch(() => {})
    }, 10000)
    return () => clearInterval(t)
  }, [absorb])

  const visible = useMemo(() => {
    if (filter === "ALL") return lines
    const min = MIN_LEVEL[filter] ?? 0
    return lines.filter((l) => (LEVEL_RANK[l.level] ?? 0) >= min)
  }, [lines, filter])

  // 自动滚到底:用户手动上滚时不打扰
  useEffect(() => {
    if (!open || paused) return
    const box = boxRef.current
    if (!box) return
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 80) {
      box.scrollTop = box.scrollHeight
    }
  }, [visible, open, paused])

  const clear = async () => {
    try { await api.clearLogs() } catch { /* ignore */ }
    seqsRef.current = new Set()
    lastSeqRef.current = 0
    setLines([])
    setErrCount(0)
  }

  return (
    <div className="shrink-0 border-t bg-card">
      <div className="flex h-9 items-center gap-2 px-4 md:px-6">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 gap-1.5 px-2"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <Terminal className="h-3.5 w-3.5" />
          <span className="text-xs font-medium">运行日志</span>
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
        </Button>

        <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">{visible.length}</Badge>
        {errCount > 0 && (
          <Badge variant="destructive" className="h-5 px-1.5 text-[10px]">错误 {errCount}</Badge>
        )}

        <div className="ml-auto flex items-center gap-1">
          {open && (
            <>
              <select
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
                aria-label="日志级别筛选"
              >
                {FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
              <Button
                variant="ghost" size="icon" className="h-7 w-7"
                onClick={() => setPaused((v) => !v)}
                title={paused ? "继续自动滚动" : "暂停自动滚动"}
                aria-label={paused ? "继续自动滚动" : "暂停自动滚动"}
              >
                {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
              </Button>
              <Button
                variant="ghost" size="icon" className="h-7 w-7"
                onClick={clear} title="清空日志" aria-label="清空日志"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </>
          )}
        </div>
      </div>

      {open && (
        <div
          ref={boxRef}
          className="h-56 overflow-auto border-t bg-muted/30 px-4 py-2 font-mono text-[11px] leading-[1.5]"
        >
          {visible.length === 0 ? (
            <div className="py-6 text-center text-muted-foreground">暂无日志</div>
          ) : (
            visible.map((l) => (
              <div key={l.seq} className="flex gap-2 whitespace-pre-wrap break-all">
                <span className="shrink-0 text-muted-foreground">{l.ts}</span>
                <span className={cn("shrink-0 w-12", LEVEL_STYLE[l.level] ?? LEVEL_STYLE.INFO)}>{l.level}</span>
                <span className="w-16 shrink-0 truncate text-muted-foreground/70" title={l.name}>{l.name}</span>
                <span className={cn(LEVEL_STYLE[l.level] ?? LEVEL_STYLE.INFO)}>{l.msg}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
