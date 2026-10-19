import { useCallback, useEffect, useRef, useState } from "react"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { LoadingState, ErrorState } from "@/components/common/states"
import { api, type PostPlayResult } from "@/lib/api"
import { Play, ExternalLink } from "lucide-react"

/** 作品播放:点封面弹这个,拿后端现取的 CDN 直链播放。
 *
 * 地址带时效(视频号 CDN 会过期),所以每次打开都问后端要一次;
 * 后端发现过期会用该视频号的浏览器现取,拿不到就明确提示原因。
 */
export function PlayDialog({ open, onOpenChange, finderId, objectId, title }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  finderId: string
  objectId: string
  title?: string
}) {
  const [data, setData] = useState<PostPlayResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const load = useCallback(async () => {
    if (!finderId || !objectId) return
    setLoading(true); setError(null); setData(null)
    try {
      const r = await api.postPlay(finderId, objectId)
      setData(r)
    } catch (e) {
      setError((e as Error).message)
    } finally { setLoading(false) }
  }, [finderId, objectId])

  useEffect(() => { if (open) load() }, [open, load])

  // 关闭时停掉播放,避免后台继续拉流
  useEffect(() => {
    if (!open && videoRef.current) {
      try { videoRef.current.pause() } catch { /* ignore */ }
    }
  }, [open])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <Play className="h-4 w-4 text-primary" />播放作品
          </DialogTitle>
          <DialogDescription className="line-clamp-2">
            {title || data?.title || "(无标题)"}
          </DialogDescription>
        </DialogHeader>

        {loading ? <div className="py-10"><LoadingState /></div>
          : error ? <ErrorState message={error} onRetry={load} />
          : data && !data.ok ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              {data.message || "无法播放这条作品"}
            </div>
          ) : data?.url ? (
            <div className="space-y-2">
              <video ref={videoRef} src={data.url} controls autoPlay playsInline
                className="max-h-[62vh] w-full rounded-md bg-black" />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {data.refreshed ? "播放地址已过期,已用当前登录态现取" : "播放地址有效"}
                </span>
                <a href={data.url} target="_blank" rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:text-foreground">
                  <ExternalLink className="h-3 w-3" />在新标签页打开
                </a>
              </div>
            </div>
          ) : null}
      </DialogContent>
    </Dialog>
  )
}
