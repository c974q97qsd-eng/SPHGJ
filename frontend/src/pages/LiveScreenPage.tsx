import { useEffect, useRef, useState } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { LoadingState, EmptyState } from "@/components/common/states"
import { useLiveScreen } from "@/hooks/useLiveScreen"
import { api, type LiveScreenItem, type MetricDef, type MetricFormat } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { Monitor, Users, ExternalLink, Settings } from "lucide-react"
import { Button } from "@/components/ui/button"
import { toast } from "sonner"
import flvjs from "flv.js"
import { CardFieldsSettings } from "@/components/CardFieldsSettings"

export function LiveScreenPage() {
  const { items, cardFields, metricDict, loading, refresh } = useLiveScreen()
  const [onlyLive, setOnlyLive] = useState(true)  // 默认仅显示直播中
  const [settingsOpen, setSettingsOpen] = useState(false)
  if (loading && items.length === 0) return <LoadingState />
  const liveItems = onlyLive ? items.filter((it) => it.is_live) : items
  if (liveItems.length === 0) {
    return (
      <Card>
        <EmptyState icon={<Users className="h-6 w-6" />} title={onlyLive ? "暂无直播中账号" : "还没有账号"}
          description={onlyLive ? "当前没有账号在直播,关闭下方开关可查看全部账号" : "请先在「账号管理」扫码添加账号并启动,直播数据会实时显示在这里"} />
      </Card>
    )
  }
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <Switch checked={onlyLive} onCheckedChange={setOnlyLive} id="only-live" />
        <label htmlFor="only-live" className="cursor-pointer text-sm text-muted-foreground">仅显示直播中</label>
        <Button variant="outline" size="sm" className="ml-auto" onClick={() => setSettingsOpen(true)}>
          <Settings className="mr-1 h-4 w-4" />卡片指标设置
        </Button>
      </div>
      <div className="flex flex-wrap gap-4">
        {liveItems.map((it) => <LiveCard key={it.account_id} it={it} cardFields={cardFields} metricDict={metricDict} />)}
      </div>
      <CardFieldsSettings open={settingsOpen} onOpenChange={setSettingsOpen}
        metricDict={metricDict} cardFields={cardFields} onSaved={refresh} />
    </div>
  )
}

function fmtDuration(sec?: number) {
  if (!sec) return "0:00"
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60)
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`
}

/** 按 metrics 字典的 format 格式化指标值。currency 值为分(÷100 显元);percent 值已乘 100。 */
function formatMetric(v: number | null | undefined, fmt: MetricFormat): string {
  if (v == null || (typeof v === "number" && Number.isNaN(v))) return "-"
  const n = Number(v)
  switch (fmt) {
    case "currency": return `¥${(n / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    case "yuan": return `¥${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    case "percent": return `${n}%`
    case "duration": return fmtDuration(n)
    case "float": return n.toFixed(2)
    default: return n.toLocaleString("zh-CN")
  }
}

function LiveCard({ it, cardFields, metricDict }: { it: LiveScreenItem; cardFields: string[]; metricDict: MetricDef[] }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const playerRef = useRef<{ destroy: () => void } | null>(null)
  const s = it.live_stats
  const metricOf = (k: string) => metricDict.find((m) => m.key === k)

  const handleOpenDashboard = async () => {
    try {
      await api.openDashboard(it.account_id)
      toast.success("已打开直播大屏,关闭后自动恢复")
    } catch (e) {
      toast.error("打开失败:" + (e as Error).message)
    }
  }

  useEffect(() => {
    // 依赖 stream_url + is_live:下播(is_live=false)或无流时销毁 player,
    // 释放 flv.js 的 MSE SourceBuffer 缓冲(否则下播后 player 残留,长跑 WebView2 JS 堆累积)。
    if (!it.is_live || !it.stream_url || !videoRef.current || !flvjs.isSupported()) {
      try { playerRef.current?.destroy() } catch { /* ignore */ }
      playerRef.current = null
      return
    }
    try {
      const p = flvjs.createPlayer({ type: "flv", url: it.stream_url, isLive: true })
      p.attachMediaElement(videoRef.current)
      p.load()
      p.play().catch(() => {})
      playerRef.current = p
    } catch (e) {
      console.error("flv.js init", e)
    }
    return () => {
      try { playerRef.current?.destroy() } catch { /* ignore */ }
      playerRef.current = null
    }
  }, [it.stream_url, it.is_live])

  // 画面下部 overlay:当前在线(liveStats)+ 自然流量 + 自然GMV(均来自 dashboardV4 metrics)
  const naturalTraffic = it.metrics?.naturalTraffic
  const naturalGmv = it.metrics?.naturalGmv

  return (
    <Card className="w-[342px] shrink-0 overflow-hidden">
      {/* 直播画面(flv.js 播放 FLV,固定 342×608 竖屏) */}
      <div className="relative h-[608px] w-full bg-black">
        {it.stream_url ? (
          <video ref={videoRef} muted autoPlay playsInline className="h-full w-full object-contain" />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-muted-foreground/70">
            <Monitor className="h-8 w-8" />
            <span className="text-xs">{it.logged_in ? "等待直播流…" : "账号未登录"}</span>
          </div>
        )}
        {/* 画面下部 overlay:当前在线 + 自然流量 + 自然GMV(半透明) */}
        <div className="absolute bottom-0 left-0 right-0 flex items-baseline justify-around gap-2 bg-black/50 px-3 py-2 text-white backdrop-blur-sm">
          <div className="flex items-baseline gap-1">
            <span className="text-xl font-bold tabular-nums">{s?.currentOnlineCount ?? 0}</span>
            <span className="text-[10px] opacity-80">当前在线</span>
          </div>
          <div className="flex items-baseline gap-1">
            <span className="text-xl font-bold tabular-nums">{naturalTraffic != null ? naturalTraffic : "-"}</span>
            <span className="text-[10px] opacity-80">自然流量</span>
          </div>
          <div className="flex items-baseline gap-1">
            <span className="text-xl font-bold tabular-nums">{naturalGmv != null ? `${naturalGmv}%` : "-"}</span>
            <span className="text-[10px] opacity-80">自然GMV</span>
          </div>
        </div>
      </div>
      {/* 直播数据 */}
      <CardContent className="space-y-2 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1 min-w-0">
            <span className="truncate text-base font-semibold">{it.name}</span>
            <Button size="icon" variant="ghost" className="h-6 w-6 shrink-0" onClick={handleOpenDashboard} aria-label="打开直播大屏" title="打开直播大屏"><ExternalLink className="h-3 w-3" /></Button>
          </div>
          {it.logged_in
            ? <Badge variant="success" className="font-normal">在线</Badge>
            : <Badge variant="secondary" className="font-normal">离线</Badge>}
        </div>
        {/* 指标网格 3×3:按 card_fields 配置渲染(全 dashboardV4 指标,可自定义) */}
        <div className="grid grid-cols-3 gap-2 text-center">
          {cardFields.slice(0, 9).map((k) => {
            const def = metricOf(k)
            if (!def) return null
            return <Stat key={k} label={def.display_name} value={formatMetric(it.metrics?.[k], def.format)} />
          })}
        </div>
        {it.updated_at && <p className="text-[10px] text-muted-foreground">更新:{fmtTime(it.updated_at)}</p>}
      </CardContent>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] text-muted-foreground">{label}</div>
    </div>
  )
}
