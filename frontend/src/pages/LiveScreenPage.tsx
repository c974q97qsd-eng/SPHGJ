import { useEffect, useRef } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { LoadingState, EmptyState } from "@/components/common/states"
import { useLiveScreen } from "@/hooks/useLiveScreen"
import type { LiveScreenItem } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { Monitor, Users } from "lucide-react"
import flvjs from "flv.js"

export function LiveScreenPage() {
  const { items, loading } = useLiveScreen()
  if (loading && items.length === 0) return <LoadingState />
  if (items.length === 0) {
    return (
      <Card>
        <EmptyState icon={<Users className="h-6 w-6" />} title="还没有账号"
          description="请先在「账号管理」扫码添加账号并启动,直播数据会实时显示在这里" />
      </Card>
    )
  }
  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((it) => <LiveCard key={it.account_id} it={it} />)}
    </div>
  )
}

function fmtDuration(sec?: number) {
  if (!sec) return "0:00"
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`
}

function LiveCard({ it }: { it: LiveScreenItem }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const playerRef = useRef<{ destroy: () => void } | null>(null)
  const s = it.live_stats

  useEffect(() => {
    if (videoRef.current && it.stream_url && flvjs.isSupported()) {
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
    }
  }, [it.stream_url])

  return (
    <Card className="overflow-hidden">
      {/* 直播画面(flv.js 播放 FLV) */}
      <div className="flex aspect-[9/16] w-full items-center justify-center bg-black">
        {it.stream_url ? (
          <video ref={videoRef} muted autoPlay playsInline className="h-full w-full object-contain" />
        ) : (
          <div className="flex flex-col items-center gap-2 text-muted-foreground/70">
            <Monitor className="h-8 w-8" />
            <span className="text-xs">{it.logged_in ? "等待直播流…" : "账号未登录"}</span>
          </div>
        )}
      </div>
      {/* 直播数据(liveStats 结构化) */}
      <CardContent className="space-y-2 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium">{it.name}</span>
          {it.logged_in
            ? <Badge variant="success" className="font-normal">在线</Badge>
            : <Badge variant="secondary" className="font-normal">离线</Badge>}
        </div>
        <div className="grid grid-cols-3 gap-2 text-center">
          <Stat label="在线" value={s?.currentOnlineCount ?? 0} />
          <Stat label="累计观看" value={s?.totalAudienceCount ?? 0} />
          <Stat label="点赞" value={s?.totalCheerCount ?? 0} />
          <Stat label="时长" value={fmtDuration(s?.liveDurationInSeconds)} />
          <Stat label="GMV" value={s?.payedGmv ?? 0} />
          <Stat label="订单" value={s?.payedNum ?? "0"} />
        </div>
        {it.updated_at && <p className="text-[10px] text-muted-foreground">更新:{fmtTime(it.updated_at)}</p>}
      </CardContent>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <div className="text-sm font-semibold">{value}</div>
      <div className="text-[10px] text-muted-foreground">{label}</div>
    </div>
  )
}
