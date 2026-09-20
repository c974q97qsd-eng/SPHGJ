import { useState } from "react"
import { Button } from "@/components/ui/button"
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import { useWebSocket, type LockConflictInfo } from "@/lib/ws"
import { toast } from "sonner"

/**
 * 「扫错微信」全局警告框。
 *
 * 后端识别到本次扫码的不是该账号锁定的微信时,会先关掉浏览器、清空该账号登录态
 * (回到全新登录环境),再推 login_lock_conflict 事件 —— 这里把它弹出来告知用户,
 * 用户确认后原地重开扫码:不必重启软件,也不必手动删 profile 目录。
 *
 * 用自绘 fixed 遮罩而不是 Radix Dialog:本警告必须压在任意已打开的登录弹窗之上,
 * 而同级 Dialog 的层级由挂载顺序决定,不可靠。
 */
export function LockConflictDialog() {
  const [info, setInfo] = useState<LockConflictInfo | null>(null)
  const [busy, setBusy] = useState(false)

  useWebSocket((e) => {
    if (e.event !== "login_lock_conflict") return
    setInfo(e.payload)
  })

  if (!info) return null

  const retry = async () => {
    setBusy(true)
    try {
      await api.loginRetryClean(info.sid)
      toast.success("登录环境已重置,请用锁定的微信重新扫码")
      setInfo(null)
    } catch (e) {
      toast.error("重开扫码失败:" + (e as Error).message + " —— 请点该账号卡片上的「重新登录」")
      setInfo(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[999] flex items-center justify-center bg-black/50 p-4">
      <div className="animate-slide-up w-full max-w-md rounded-lg border bg-background p-5 shadow-xl">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-destructive/10">
            <AlertTriangle className="h-5 w-5 text-destructive" />
          </div>
          <div className="min-w-0 flex-1 space-y-2">
            <h2 className="text-base font-semibold text-foreground">扫码的微信不对</h2>
            <div className="space-y-1 rounded-md bg-muted/50 px-3 py-2 text-xs">
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">目标账号</span>
                <span className="ml-2 truncate font-medium">{info.account_name || info.account_id || "—"}</span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">应扫的微信</span>
                <span className="ml-2 truncate font-medium text-destructive">{info.locked_name || "—"}</span>
              </div>
              {info.scanned_name && (
                <div className="flex justify-between gap-2">
                  <span className="text-muted-foreground">本次扫到的</span>
                  <span className="ml-2 truncate font-medium">{info.scanned_name}</span>
                </div>
              )}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              {info.reason}
              {info.cleared
                ? "。为避免串号,本账号的登录态文件已全部清除,现在是一个全新的登录环境。"
                : `。提示:登录态未能完全清除(${info.cleared_msg}),请先关掉可能还开着的浏览器窗口再重扫。`}
            </p>
            <p className="text-xs font-medium text-foreground">
              请用微信「{info.locked_name || "锁定账号"}」重新扫码。
            </p>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-2">
          {info.can_retry && (
            <p className="mr-auto text-[11px] text-muted-foreground">10 分钟内未处理,本次登录将自动结束</p>
          )}
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setInfo(null)}>
            稍后处理
          </Button>
          {info.can_retry && (
            <Button size="sm" className="gap-1.5" disabled={busy} onClick={retry}>
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              用锁定的微信重新扫码
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
