import { useEffect, useState, type ReactNode } from "react"
import { NavLink, useLocation } from "react-router-dom"
import {
  LayoutDashboard, Users, MessageSquare, Reply, Moon, Sun,
  Play, Square, RefreshCw, Loader2, Trash2, Monitor,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { AdSlot } from "@/components/common/ad-slot"
import { TextAdSlot } from "@/components/common/text-ad-slot"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useTheme } from "@/lib/theme"
import { api } from "@/lib/api"
import { useAccounts } from "@/hooks/useAccounts"
import { cn } from "@/lib/utils"
import { toast } from "sonner"

const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/accounts", label: "账号管理", icon: Users },
  { to: "/comments", label: "评论", icon: MessageSquare },
  { to: "/auto-reply", label: "自动回复", icon: Reply },
  { to: "/auto-delete", label: "自动删除", icon: Trash2 },
  { to: "/live-screen", label: "直播大屏", icon: Monitor },
]

const TITLES: Record<string, string> = { "/": "仪表盘", "/accounts": "账号管理", "/comments": "评论", "/auto-reply": "自动回复设置", "/auto-delete": "自动删除", "/live-screen": "直播大屏" }

function EngineControls() {
  const { status, refresh } = useAccounts()
  const [interval, setInterval_] = useState(String(status && 300))
  const [acting, setActing] = useState(false)
  const [configInterval, setConfigInterval] = useState(300)

  useEffect(() => {
    api.getConfig().then((c) => { setConfigInterval(c.fetch_interval_sec); setInterval_(String(c.fetch_interval_sec)) }).catch(() => {})
  }, [])

  const start = async () => { setActing(true); try { await api.engineStart(); toast.success("引擎已启动") } catch (e) { toast.error("启动失败:" + (e as Error).message) } finally { setActing(false); refresh() } }
  const stop = async () => { setActing(true); try { await api.engineStop(); toast.success("引擎已停止") } catch (e) { toast.error("停止失败:" + (e as Error).message) } finally { setActing(false); refresh() } }
  const fetchNow = async () => { setActing(true); try { await api.engineFetchNow(); toast.success("已触发立即抓取") } catch (e) { toast.error((e as Error).message) } finally { setActing(false) } }
  const saveInterval = async () => {
    const v = parseInt(interval, 10)
    if (isNaN(v) || v < 10) { toast.error("间隔需 ≥10 秒"); return }
    try { await api.patchConfig({ fetch_interval_sec: v }); setConfigInterval(v); toast.success("间隔已保存") } catch (e) { toast.error((e as Error).message) }
  }

  return (
    <div className="flex items-center gap-2">
      {status.running ? (
        <Button size="sm" variant="destructive" onClick={stop} disabled={acting}>
          {acting ? <Loader2 className="animate-spin" /> : <Square />}停止
        </Button>
      ) : (
        <Button size="sm" variant="success" onClick={start} disabled={acting}>
          {acting ? <Loader2 className="animate-spin" /> : <Play />}启动
        </Button>
      )}
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="sm" variant="outline" onClick={fetchNow} disabled={acting || !status.running}>
              {acting ? <Loader2 className="animate-spin" /> : <RefreshCw />}立即抓取
            </Button>
          </TooltipTrigger>
          <TooltipContent>立即抓取所有在线账号</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <div className="hidden sm:flex items-center gap-1.5 ml-1">
        <span className="text-xs text-muted-foreground">间隔</span>
        <Input className="h-8 w-16 text-xs" value={interval} onChange={(e) => setInterval_(e.target.value)} onBlur={saveInterval} onKeyDown={(e) => e.key === "Enter" && saveInterval()} inputMode="numeric" aria-label="抓取间隔秒数" />
        <span className="text-xs text-muted-foreground">秒</span>
      </div>
    </div>
  )
}

function ThemeToggle() {
  const { theme, toggle } = useTheme()
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon" onClick={toggle} aria-label="切换主题">
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </TooltipTrigger>
        <TooltipContent>切换{theme === "dark" ? "浅色" : "深色"}主题</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

export function AppShell({ children }: { children: ReactNode }) {
  const loc = useLocation()
  const title = TITLES[loc.pathname] || "视频号工具"
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      {/* 侧栏 */}
      <aside className="hidden md:flex w-[200px] shrink-0 flex-col border-r bg-card">
        <div className="flex h-14 items-center gap-2 px-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <MessageSquare className="h-4 w-4" />
          </div>
          <span className="font-semibold tracking-tight">视频号工具</span>
        </div>
        <Separator />
        <nav className="flex-1 space-y-1 p-3" aria-label="主导航">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <Separator />
        <div className="p-3">
          <div className="rounded-md bg-muted/50 p-3 text-xs text-muted-foreground">
            <p className="font-medium text-foreground mb-1">视频号评论区</p>
            <p>多账号 · 增量抓取 · 自动回复</p>
          </div>
        </div>
      </aside>

      {/* 主区 */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-card px-4 md:px-6">
          <div className="flex items-center gap-2">
            {/* 移动端导航(下拉简化) */}
            <nav className="flex md:hidden items-center gap-1" aria-label="移动导航">
              {NAV.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => cn("rounded-md p-2", isActive ? "bg-accent text-accent-foreground" : "text-muted-foreground")}>
                  <item.icon className="h-4 w-4" />
                </NavLink>
              ))}
            </nav>
            <h1 className="text-base font-semibold">{title}</h1>
            <span className="hidden text-xs text-muted-foreground sm:inline whitespace-nowrap">软件定制加17070860806</span>
          </div>
          <TextAdSlot />
          <div className="flex items-center gap-2">
            <EngineControls />
            <Separator orientation="vertical" className="h-6" />
            <ThemeToggle />
          </div>
        </header>
        <AdSlot />
        <main className="flex-1 overflow-auto p-4 md:p-6">{children}</main>
      </div>
    </div>
  )
}
