import { BrowserRouter, Routes, Route } from "react-router-dom"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Toaster } from "@/components/ui/sonner"
import { ThemeProvider } from "@/lib/theme"
import { AppShell } from "@/components/layout/AppShell"
import { DashboardPage } from "@/pages/DashboardPage"
import { AccountsPage } from "@/pages/AccountsPage"
import { CommentsPage } from "@/pages/CommentsPage"
import { AutoReplyPage } from "@/pages/AutoReplyPage"
import { LiveScreenPage } from "@/pages/LiveScreenPage"
import { AutoDeletePage } from "@/pages/AutoDeletePage"

export default function App() {
  return (
    <ThemeProvider>
      <TooltipProvider delayDuration={200}>
        <BrowserRouter>
          <AppShell>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/accounts" element={<AccountsPage />} />
              <Route path="/comments" element={<CommentsPage />} />
              <Route path="/auto-reply" element={<AutoReplyPage />} />
              <Route path="/auto-delete" element={<AutoDeletePage />} />
              <Route path="/live-screen" element={<LiveScreenPage />} />
            </Routes>
          </AppShell>
          <Toaster />
        </BrowserRouter>
      </TooltipProvider>
    </ThemeProvider>
  )
}
