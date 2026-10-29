import { useEffect, useState, type ReactNode } from "react"
import { NavLink, useLocation } from "react-router-dom"
import {
  LayoutDashboard, Users, MessageSquare, Moon, Sun,
  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard, Send, Power,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { AdSlot } from "@/components/common/ad-slot"
import { TextAdSlot } from "@/components/common/text-ad-slot"
import { LogPanel } from "@/components/common/LogPanel"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useTheme } from "@/lib/theme"
import { useUiScale, UI_SCALE_OPTIONS, type UiScale } from "@/lib/uiScale"
import { api } from "@/lib/api"
import { useAccounts } from "@/hooks/useAccounts"
import { cn } from "@/lib/utils"
import { toast } from "sonner"

// rev22:「自动回复」「自动删除」并入「评论管理」,不再单列导航项
const NAV = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/accounts", label: "账号管理", icon: Users },
  { to: "/comments", label: "评论管理", icon: MessageSquare },
  { to: "/posts", label: "作品管理", icon: Clapperboard },
  { to: "/publish", label: "发布管理", icon: Send },
  { to: "/live-screen", label: "直播大屏", icon: Monitor },
]

const TITLES: Record<string, string> = { "/": "仪表盘", "/accounts": "账号管理", "/comments": "评论管理", "/auto-reply": "评论管理", "/auto-delete": "评论管理", "/live-screen": "直播大屏", "/posts": "作品管理", "/publish": "发布管理" }

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
  /**
   * 重启软件。waitTask=true:有作品正在发布时后端会等它发完再退,
   * 接口立刻返回 state="waiting",这里只管轮询 pid,变了就刷新页面。
   */
  const restart = async () => {
    setActing(true)
    let oldPid: number | null = null
    try { oldPid = (await api.systemInfo()).pid ?? null } catch { /* 拿不到就靠状态断 */ }
    try {
      const r = await api.systemRestart({ waitTask: true, delay: 3 })
      if (r.state === "waiting") toast.info(r.hint || "有作品正在发布,发完再重启")
      // rev59: 120s 内重复点不会起第二个 helper,如实提示,避免"点了两次以为重启两次"
      else if (r.state === "already") toast.info(r.hint || "已经在重启中,继续等它完成")
      else toast.info("正在重启,完成后页面会自动刷新")
    } catch (e) {
      toast.error("重启失败:" + (e as Error).message)
      setActing(false)
      return
    }
    const deadline = Date.now() + 5 * 60 * 1000
    while (Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 2000))
      try {
        const info = await api.systemInfo()
        if (info.pid && info.pid !== oldPid) {
          toast.success("已重启,正在刷新")
          setTimeout(() => location.reload(), 700)
          return
        }
      } catch { /* 重启中连不上是正常的,继续等 */ }
    }
    toast.error("等重启超时了,请手动刷新页面")
    setActing(false)
  }
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
      {/* rev58: 重启系统。有作品在发就先发完(后端 wait_task),队列里其余任务不受影响 */}
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="sm" variant="outline" onClick={restart} disabled={acting}>
              {acting ? <Loader2 className="animate-spin" /> : <Power />}重启
            </Button>
          </TooltipTrigger>
          <TooltipContent>重启软件(改代码/配置后生效)。正在发布的作品会先发完再重启,排队中的任务不受影响</TooltipContent>
        </Tooltip>
      </TooltipProvider>
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

/** 界面缩放(DPI 档位):大 / 中 / 小。改根字号整体等比缩放,即时生效并持久化。 */
function UiScaleSelect() {
  const { scale, setScale } = useUiScale()
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div>
            <Select value={scale} onValueChange={(v) => setScale(v as UiScale)}>
              <SelectTrigger className="h-8 w-[5.75rem] gap-1 px-2 text-xs" aria-label="界面缩放">
                <Type className="h-3.5 w-3.5 shrink-0" />
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {UI_SCALE_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value} className="text-xs">
                    显示 · {o.label}（{o.hint}）
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </TooltipTrigger>
        <TooltipContent>界面缩放:1080P 屏建议「中」或「小」,一屏显示更多</TooltipContent>
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
      {/* 宽度用 rem(12.5rem = 大档位的 200px),这样切到中/小档时侧栏跟着一起缩 */}
      <aside className="hidden md:flex w-[12.5rem] shrink-0 flex-col border-r bg-card">
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
            <p>多账号 · 增量抓取 · 评论管理</p>
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
            <UiScaleSelect />
            <ThemeToggle />
          </div>
        </header>
        {/* 广告放在滚动容器(main)内部,跟随页面一起滚动;置于 main 之外则会被固定置顶 */}
        <main className="flex-1 overflow-auto p-4 md:p-6">
          <AdSlot />
          {children}
        </main>
        {/* 运行日志:无控制台运行时(打包 exe / pythonw)唯一能看日志的地方 */}
        <LogPanel />
      </div>
    </div>
  )
}
