import { useEffect, useRef, useState } from "react"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Loader2, ExternalLink, AlertCircle, Smartphone, Monitor, QrCode } from "lucide-react"
import { api } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"
import { toast } from "sonner"

type Status = "starting" | "waiting_scan" | "scanned" | "capturing" | "captured" | "failed" | "cancelled"

const STATUS_TEXT: Record<Status, string> = {
  starting: "正在准备登录环境…",
  waiting_scan: "等待扫码",
  scanned: "扫码成功,正在抓取并保存…",
  capturing: "扫码成功,正在抓取并保存…",
  captured: "扫码成功,正在保存账号…",
  failed: "登录失败",
  cancelled: "已取消",
}

/** auto=后端按客户端是否本机自动决定(未拿到结果前不高亮任何按钮) */
type ScanMode = "auto" | "window" | "web"

export function AddAccountDialog({ open, onOpenChange, onDone, reloginAccountId, reloginAccountName, reloginWxName }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
  reloginAccountId?: string
  reloginAccountName?: string
  reloginWxName?: string
}) {
  const [sid, setSid] = useState<string | null>(null)
  const [status, setStatus] = useState<Status>("starting")
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<ScanMode>("auto")
  const [qrImage, setQrImage] = useState<string | null>(null)
  const sidRef = useRef<string | null>(null)
  sidRef.current = sid
  const finalizingRef = useRef(false)

  const startLogin = async (scanMode?: ScanMode) => {
    // 重试时先清理旧会话(异步,不阻塞)
    const old = sidRef.current
    if (old) { void api.loginCancel(old).catch(() => {}) }
    finalizingRef.current = false
    setStatus("starting"); setError(null); setSid(null); setQrImage(null)
    if (scanMode) setMode(scanMode)
    try {
      // 未指定模式 -> 不传 headed,由后端按客户端是否本机自动决定:
      // 本机走浏览器窗口弹窗,远程(局域网其他主机)走 headless + 网页二维码
      const headed = scanMode ? scanMode === "window" : undefined
      const r = reloginAccountId
        ? await api.reloginAccount(reloginAccountId, headed)
        : await api.loginStart(headed)
      setSid(r.sid)
      setStatus((r.status as Status) || "waiting_scan")
      // 跟随后端实际采用的模式
      if (r.headed !== undefined) setMode(r.headed ? "window" : "web")
    } catch (e) {
      setError((e as Error).message)
      setStatus("failed")
    }
  }

  // 开启 -> 发起登录
  useEffect(() => {
    if (!open) return
    startLogin()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // 自动保存:收到 captured 调 finalize(后端关弹窗 + 落盘)
  const autoFinalize = async (name?: string) => {
    const s = sidRef.current
    if (!s) return
    try {
      const r = await api.loginFinalize(s, reloginAccountId, name || undefined)
      const acc = r?.account as { _dedup_updated?: boolean; id?: string; name?: string } | undefined
      if (reloginAccountId) {
        await api.startAccount(reloginAccountId)
        toast.success("已重新登录并启动")
      } else if (acc?._dedup_updated && acc.id) {
        await api.startAccount(acc.id).catch(() => {})
        toast.success(`已更新账号${acc.name ? ` ${acc.name}` : ""} 并启动`)
      } else {
        toast.success("账号已添加")
      }
      onOpenChange(false)
      onDone()
    } catch (e) {
      setError("保存失败:" + (e as Error).message)
      setStatus("failed")
    }
  }

  // WS:登录状态(按 sid 过滤) + 二维码推送
  useWebSocket((e: WsEvent) => {
    const mySid = sidRef.current
    if (!mySid) return
    if (e.event === "login_status" && e.payload.sid === mySid) {
      const p = e.payload
      setStatus(p.status as Status)
      if (p.status === "captured" && !finalizingRef.current) {
        finalizingRef.current = true
        void autoFinalize(p.captured?.name)
      }
      if (p.status === "failed") setError(p.error || "登录失败")
    }
    if (e.event === "qr_update" && e.payload.sid === mySid) {
      setQrImage(e.payload.image || null)
      // 收到二维码推送 => 后端必然走的 headless 网页模式(兼容后端未返回 headed 字段)
      setMode((m) => (m === "auto" ? "web" : m))
    }
  })

  const reopen = async () => {
    const s = sidRef.current
    if (!s) return
    try { await api.loginOpenWindow(s) } catch (e) { toast.error((e as Error).message) }
  }
  const cancel = async () => {
    const s = sidRef.current
    if (s) { try { await api.loginCancel(s) } catch {} }
    onOpenChange(false)
  }

  const processing = status === "starting" || status === "scanned" || status === "capturing" || status === "captured"
  const showScan = status === "waiting_scan"

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) cancel(); else onOpenChange(true) }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Smartphone className="h-5 w-5 text-primary" />{reloginAccountId ? "重新登录" : "添加账号"}</DialogTitle>
          <DialogDescription className="sr-only">扫码登录视频号助手,软件自动抓取账号信息并保存</DialogDescription>
        </DialogHeader>

        {/* 当前要登录的账号 + 微信号(方便用户确认用哪个微信扫哪个账号) */}
        <div className="rounded-md bg-muted/40 px-3 py-2 text-xs space-y-0.5">
          <div className="flex justify-between gap-2"><span className="text-muted-foreground">登录账号</span><span className="font-medium truncate ml-2">{reloginAccountName || "新账号"}</span></div>
          {reloginWxName && <div className="flex justify-between gap-2"><span className="text-muted-foreground">原微信号</span><span className="font-medium truncate ml-2">{reloginWxName}</span></div>}
        </div>

        {/* 扫码模式选择(仅在 waiting_scan 或 starting 时可切) */}
        {(showScan || status === "starting") && (
          <div className="flex items-center justify-center gap-1 rounded-md border bg-muted/30 p-1">
            <button
              onClick={() => startLogin("window")}
              className={`flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium transition-colors ${mode === "window" ? "bg-background shadow-sm" : "hover:bg-muted/60"}`}
            >
              <Monitor className="h-3.5 w-3.5" />本机窗口
            </button>
            <button
              onClick={() => startLogin("web")}
              className={`flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium transition-colors ${mode === "web" ? "bg-background shadow-sm" : "hover:bg-muted/60"}`}
            >
              <QrCode className="h-3.5 w-3.5" />网页二维码
            </button>
          </div>
        )}

        <div className="flex flex-col items-center gap-3 py-6">
          {status === "failed" ? <AlertCircle className="h-10 w-10 text-destructive" />
            : processing ? <Loader2 className="h-10 w-10 animate-spin text-primary" />
            : <Smartphone className="h-10 w-10 text-primary" />}
          <p className={`text-sm font-medium text-center ${status === "failed" ? "text-destructive" : "text-foreground"}`}>
            {error ? error : STATUS_TEXT[status]}
          </p>
          {showScan && (
            <>
              {/* 网页二维码模式:渲染二维码图片 */}
              {mode === "web" && qrImage ? (
                <div className="flex flex-col items-center gap-2">
                  <img src={qrImage} alt="扫码登录" className="rounded-lg border shadow-sm max-w-[220px]" />
                  <p className="text-xs text-muted-foreground text-center max-w-[280px]">
                    用微信扫描上方二维码,确认后自动抓取账号信息
                  </p>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground text-center max-w-[280px]">
                  {mode === "web"
                    ? "正在获取二维码…"
                    : "已弹出浏览器窗口,用微信扫码并在手机确认后,软件自动抓取账号信息并保存"}
                </p>
              )}
            </>
          )}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={cancel}>取消</Button>
          {showScan && mode === "window" && (
            <Button variant="outline" onClick={reopen} className="gap-1.5">
              <ExternalLink className="h-3.5 w-3.5" />重新打开扫码窗口
            </Button>
          )}
          {status === "failed" && (
            <Button onClick={() => startLogin()} className="gap-1.5">重试</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
