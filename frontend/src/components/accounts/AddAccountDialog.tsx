import { useEffect, useRef, useState } from "react"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Loader2, ExternalLink, AlertCircle, Smartphone } from "lucide-react"
import { api } from "@/lib/api"
import { useWebSocket, type WsEvent } from "@/lib/ws"
import { toast } from "sonner"

type Status = "starting" | "waiting_scan" | "scanned" | "capturing" | "captured" | "failed" | "cancelled"

const STATUS_TEXT: Record<Status, string> = {
  starting: "正在准备登录环境…",
  waiting_scan: "请在弹出的浏览器窗口扫码",
  scanned: "扫码成功,正在抓取并保存…",
  capturing: "扫码成功,正在抓取并保存…",
  captured: "扫码成功,正在保存账号…",
  failed: "登录失败",
  cancelled: "已取消",
}

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
  const sidRef = useRef<string | null>(null)
  sidRef.current = sid
  const finalizingRef = useRef(false)

  const startLogin = async () => {
    // 重试时先清理旧会话(异步,不阻塞)
    const old = sidRef.current
    if (old) { void api.loginCancel(old).catch(() => {}) }
    finalizingRef.current = false
    setStatus("starting"); setError(null); setSid(null)
    try {
      const r = reloginAccountId ? await api.reloginAccount(reloginAccountId) : await api.loginStart()
      setSid(r.sid)
      setStatus((r.status as Status) || "waiting_scan")
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
        // 排重更新:后端已更新已有账号字段+cookie,这里重启加载新 cookie
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

  // WS:登录状态(按 sid 过滤)
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

        <div className="flex flex-col items-center gap-3 py-6">
          {status === "failed" ? <AlertCircle className="h-10 w-10 text-destructive" />
            : processing ? <Loader2 className="h-10 w-10 animate-spin text-primary" />
            : <Smartphone className="h-10 w-10 text-primary" />}
          <p className={`text-sm font-medium text-center ${status === "failed" ? "text-destructive" : "text-foreground"}`}>
            {error ? error : STATUS_TEXT[status]}
          </p>
          {showScan && (
            <p className="text-xs text-muted-foreground text-center max-w-[280px]">
              已弹出浏览器窗口,用微信扫码并在手机确认后,软件自动抓取账号信息并保存
            </p>
          )}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={cancel}>取消</Button>
          {showScan && (
            <Button variant="outline" onClick={reopen} className="gap-1.5">
              <ExternalLink className="h-3.5 w-3.5" />重新打开扫码窗口
            </Button>
          )}
          {status === "failed" && (
            <Button onClick={startLogin} className="gap-1.5">重试</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
