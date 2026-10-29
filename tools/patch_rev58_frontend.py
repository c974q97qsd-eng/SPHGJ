"""rev58 一次性补丁:前端(账号选项卡片接入三处 + 排序口径 + 顶栏重启按钮)。"""
import io
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def patch(rel, patches):
    p = os.path.join(ROOT, rel)
    s = io.open(p, encoding="utf-8").read()
    for tag, old, new in patches:
        n = s.count(old)
        assert n == 1, f"[{rel}:{tag}] 命中 {n} 次,期望 1 次"
        s = s.replace(old, new, 1)
        print(f"  ok  {rel}  {tag}")
    io.open(p, "w", encoding="utf-8", newline="").write(s)
    return s


# ==================== frontend/src/lib/api.ts ====================
patch("frontend/src/lib/api.ts", [
    ("PublishFinder 加 counts",
     '''  last_success_at: string
  fail_streak: number
}''',
     '''  last_success_at: string
  fail_streak: number
  /** rev58: 各状态任务数 {"pending":n,"success":n,...},卡片上的数字由它求和 */
  counts: Record<string, number>
}'''),
    ("publishTasks 加 order",
     '''  publishTasks: (params: { status?: string; finder_id?: string; limit?: number; offset?: number } = {}) => {''',
     '''  publishTasks: (params: { status?: string; finder_id?: string; limit?: number; offset?: number; order?: "queue" | "history" } = {}) => {'''),
    ("systemInfo / systemRestart",
     '''  whoami: () => req<{ local: boolean; client: string }>("/system/whoami"),''',
     '''  whoami: () => req<{ local: boolean; client: string }>("/system/whoami"),
  systemInfo: () =>
    req<{ ok: boolean; pid: number; version: string; started_at?: string;
          restart_wait?: { waiting: boolean; since: number; max_wait: number } }>("/system/info"),
  /**
   * 重启软件(改代码/配置后生效)。
   * waitTask=true:若有作品正在发布,后端等它发完才退出(最多 maxWaitSec 秒),
   * 返回 state="waiting";否则立刻退,state="restarting"。
   */
  systemRestart: (params: { waitTask?: boolean; delay?: number; maxWaitSec?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.delay !== undefined) q.set("delay", String(params.delay))
    if (params.waitTask) q.set("wait_task", "1")
    if (params.maxWaitSec !== undefined) q.set("max_wait", String(params.maxWaitSec))
    return req<{ ok: boolean; state: "waiting" | "restarting"; hint?: string; already?: boolean; max_wait?: number }>(
      `/system/restart?${q.toString()}`, { method: "POST" })
  },'''),
])

# ==================== TaskTable.tsx ====================
patch("frontend/src/components/publish/TaskTable.tsx", [
    ("TaskTable 加 finderId",
     '''export function TaskTable({ mode }: { mode: "queue" | "history" }) {''',
     '''export function TaskTable({ mode, finderId = "" }: { mode: "queue" | "history"; finderId?: string }) {'''),
    ("load 带 finder_id + order",
     '''      const r = await api.publishTasks({ status: statusParam, limit: PAGE_SIZE, offset })''',
     '''      // order 交给后端:界面要「最新的在最上面」,而内部调度仍按「老任务先发」取,
      // 两边共用一条 SQL 会打架,所以排序口径由调用方指定(见 storage.query_tasks)。
      const r = await api.publishTasks({
        status: statusParam, finder_id: finderId, order: mode,
        limit: PAGE_SIZE, offset,
      })'''),
    ("load 依赖 finderId",
     '''  }, [statusParam, offset])''',
     '''  }, [statusParam, offset, finderId, mode])'''),
    ("切账号回第一页",
     '''  // 把当前页删空时自动回退一页，否则会停在一张空表格上''',
     '''  // 切账号要回到第一页:否则带着上一个账号的 offset,大概率落在空页上
  useEffect(() => { setOffset(0) }, [finderId])

  // 把当前页删空时自动回退一页，否则会停在一张空表格上'''),
    ("空态区分账号",
     '''  if (!rows.length) {
    return (
      <EmptyState
        icon={mode === "queue" ? <Inbox className="h-5 w-5" /> : <History className="h-5 w-5" />}
        title={mode === "queue" ? "没有待发布的作品" : "还没有发布记录"}
        description={mode === "queue"
          ? "把视频丢进「视频号」子目录,系统会自动认领归属并从标题池取一条标题"
          : "发布成功或失败后会在这里留档,可核对错误原因"}
      />
    )
  }''',
     '''  if (!rows.length) {
    const scoped = !!finderId          // 已按账号筛选:空态要说清是「这个账号没有」
    return (
      <EmptyState
        icon={mode === "queue" ? <Inbox className="h-5 w-5" /> : <History className="h-5 w-5" />}
        title={mode === "queue"
          ? (scoped ? "这个账号没有待发布的作品" : "没有待发布的作品")
          : (scoped ? "这个账号还没有发布记录" : "还没有发布记录")}
        description={mode === "queue"
          ? "把视频丢进「视频号」子目录,系统会自动认领归属并从标题池取一条标题"
          : "发布成功或失败后会在这里留档,可核对错误原因"}
      />
    )
  }'''),
])

# ==================== QueuePanel.tsx ====================
patch("frontend/src/components/publish/QueuePanel.tsx", [
    ("import",
     '''import { TaskTable } from "@/components/publish/TaskTable"''',
     '''import { TaskTable } from "@/components/publish/TaskTable"
import { AccountPicker } from "@/components/publish/AccountPicker"
import { pendingCountOf } from "@/hooks/usePublishFinders"'''),
    ("图标",
     '''import { FolderOpen, RefreshCw, AlertTriangle, Send, CheckCircle2, Clock } from "lucide-react"''',
     '''import { FolderOpen, RefreshCw, AlertTriangle, Send, CheckCircle2, Clock, Users, CircleUser } from "lucide-react"'''),
    ("state",
     '''  const [bindDir, setBindDir] = useState<Record<string, string>>({})''',
     '''  const [bindDir, setBindDir] = useState<Record<string, string>>({})
  // rev58: 账号筛选。"" 不安全(Radix Tabs 要非空 value),用哨兵表示「全部」
  const [finder, setFinder] = useState("__all__")'''),
    ("账号卡片 + 传 finderId",
     '''      <TaskTable mode="queue" />''',
     '''      {/* 账号选项卡片:同款页签胶囊,选中哪个账号下面表格就只看哪个 */}
      <div className="flex flex-wrap items-center gap-2">
        <AccountPicker
          value={finder}
          onChange={setFinder}
          ariaLabel="按账号筛选待发布"
          options={[
            {
              value: "__all__", label: "全部", icon: <Users className="h-3.5 w-3.5" />,
              count: finders.reduce((a, f) => a + pendingCountOf(f), 0),
              hint: "所有视频号的待发布合计",
            },
            ...finders.map((f) => ({
              value: f.finder_id,
              label: f.name,
              icon: <CircleUser className="h-3.5 w-3.5" />,
              count: pendingCountOf(f),
              hint: `${f.name} · 待发 ${pendingCountOf(f)} 条` +
                (f.today_success ? ` · 今日已发 ${f.today_success} 条` : "") +
                (f.running ? " · 账号运行中" : " · 账号未运行"),
            })),
          ]}
        />
      </div>

      <TaskTable mode="queue" finderId={finder === "__all__" ? "" : finder} />'''),
])

# ==================== TitlesPanel.tsx ====================
patch("frontend/src/components/publish/TitlesPanel.tsx", [
    ("去掉 Select 导入",
     '''import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
''',
     ''''''),
    ("换成 AccountPicker 导入",
     '''import { LoadingState, ErrorState, EmptyState } from "@/components/common/states"''',
     '''import { AccountPicker } from "@/components/publish/AccountPicker"
import { LoadingState, ErrorState, EmptyState } from "@/components/common/states"'''),
    ("图标",
     '''import { Plus, Trash2, ListChecks, Eraser } from "lucide-react"''',
     '''import { Plus, Trash2, ListChecks, Eraser, Layers, CircleUser } from "lucide-react"'''),
    ("下拉框换成账号卡片",
     '''            <Select value={pool || "__generic__"} onValueChange={(v) => setPool(v === "__generic__" ? "" : v)}>
              <SelectTrigger className="h-8 w-56 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__generic__">通用池(所有视频号兜底) · 剩 {generic.unused}/{generic.total}</SelectItem>
                {finders.map((f) => (
                  <SelectItem key={f.finder_id} value={f.finder_id}>
                    {f.name} · 剩 {f.titles_unused}/{f.titles_total}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>''',
     '''            {/* rev58: 下拉换成账号卡片(pool="" 是通用池,不能用空值当 Tabs value) */}
            <AccountPicker
              value={pool || "__generic__"}
              onChange={(v) => setPool(v === "__generic__" ? "" : v)}
              ariaLabel="按账号筛选标题池"
              options={[
                {
                  value: "__generic__", label: "通用池", icon: <Layers className="h-3.5 w-3.5" />,
                  count: generic.unused,
                  hint: `通用池(所有视频号兜底) · 剩 ${generic.unused}/${generic.total}`,
                },
                ...finders.map((f) => ({
                  value: f.finder_id,
                  label: f.name,
                  icon: <CircleUser className="h-3.5 w-3.5" />,
                  count: f.titles_unused,
                  hint: `${f.name} · 剩 ${f.titles_unused}/${f.titles_total}` +
                    (f.today_success ? ` · 今日已发 ${f.today_success} 条` : ""),
                })),
              ]}
            />'''),
])

# ==================== AppShell.tsx ====================
patch("frontend/src/components/layout/AppShell.tsx", [
    ("图标",
     '''  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard, Send,''',
     '''  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard, Send, Power,'''),
    ("restart 函数",
     '''  const fetchNow = async () => {''',
     '''  /**
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
  const fetchNow = async () => {'''),
    ("重启按钮",
     '''      {status.running ? (
        <Button size="sm" variant="destructive" onClick={stop} disabled={acting}>
          {acting ? <Loader2 className="animate-spin" /> : <Square />}停止
        </Button>
      ) : (
        <Button size="sm" variant="success" onClick={start} disabled={acting}>
          {acting ? <Loader2 className="animate-spin" /> : <Play />}启动
        </Button>
      )}''',
     '''      {status.running ? (
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
      </TooltipProvider>'''),
])

print("ALL PATCHES OK")
