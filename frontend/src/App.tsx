import { BrowserRouter, Routes, Route } from "react-router-dom"
import { useRef } from "react"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Toaster } from "@/components/ui/sonner"
import { ThemeProvider } from "@/lib/theme"
import { UiScaleProvider } from "@/lib/uiScale"
import { AppShell } from "@/components/layout/AppShell"
import { LockConflictDialog } from "@/components/accounts/LockConflictDialog"
import { useWebSocket } from "@/lib/ws"
import { toast } from "sonner"
import { DashboardPage } from "@/pages/DashboardPage"
import { AccountsPage } from "@/pages/AccountsPage"
import { CommentsPage } from "@/pages/CommentsPage"
import { AutoReplyPage } from "@/pages/AutoReplyPage"
import { LiveScreenPage } from "@/pages/LiveScreenPage"
import { AutoDeletePage } from "@/pages/AutoDeletePage"
import { PostsPage } from "@/pages/PostsPage"

/** 失效账号自动依次扫码进度通知:后端驱动弹窗,前端只提示进度。 */
function AutoReloginNotifier() {
  const lastActive = useRef<string | null>(null)
  useWebSocket((e) => {
    if (e.event !== "relogin_queue") return
    const { active, active_name, pending } = e.payload as {
      active: string | null; active_name?: string; pending: string[]
    }
    if (active && active !== lastActive.current) {
      lastActive.current = active
      toast.info(`正在重新登录账号「${active_name || active}」${pending.length ? `,还剩 ${pending.length} 个` : ""}`)
    } else if (!active) {
      lastActive.current = null
    }
  })
  return null
}

export default function App() {
  return (
    <ThemeProvider>
      <UiScaleProvider>
        <TooltipProvider delayDuration={200}>
          <BrowserRouter>
            <AppShell>
              <AutoReloginNotifier />
              {/* 扫错微信全局警告:清除登录态后提示用锁定的微信重扫(任意页面都能弹) */}
              <LockConflictDialog />
              <Routes>
                <Route path="/" element={<DashboardPage />} />
                <Route path="/accounts" element={<AccountsPage />} />
                <Route path="/comments" element={<CommentsPage />} />
                <Route path="/auto-reply" element={<AutoReplyPage />} />
                <Route path="/auto-delete" element={<AutoDeletePage />} />
                <Route path="/live-screen" element={<LiveScreenPage />} />
                <Route path="/posts" element={<PostsPage />} />
              </Routes>
            </AppShell>
            <Toaster />
          </BrowserRouter>
        </TooltipProvider>
      </UiScaleProvider>
    </ThemeProvider>
  )
}
