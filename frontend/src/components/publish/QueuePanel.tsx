import { useCallback, useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Switch } from "@/components/ui/switch"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { TaskTable } from "@/components/publish/TaskTable"
import { AccountPicker } from "@/components/publish/AccountPicker"
import { pendingCountOf } from "@/hooks/usePublishFinders"
import { api, type PublishFinder, type PublishStatus } from "@/lib/api"
import { toast } from "sonner"
import { FolderOpen, RefreshCw, AlertTriangle, Send, CheckCircle2, Clock, Users, CircleUser } from "lucide-react"

/** 待发布:状态概览 + inbox 认领 + 队列表。 */
export function QueuePanel() {
  const [st, setSt] = useState<PublishStatus | null>(null)
  const [finders, setFinders] = useState<PublishFinder[]>([])
  const [scanning, setScanning] = useState(false)
  const [bindDir, setBindDir] = useState<Record<string, string>>({})
  // rev58: 账号筛选。"" 不安全(Radix Tabs 要非空 value),用哨兵表示「全部」
  const [finder, setFinder] = useState("__all__")

  const load = useCallback(async () => {
    try {
      const [s, f] = await Promise.all([api.publishStatus(), api.publishFinders()])
      setSt(s)
      setFinders(f.items)
    } catch (e) {
      toast.error((e as Error).message)
    }
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => {
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [load])

  const toggle = async (on: boolean) => {
    try { await api.publishSaveSettings({ enabled: on }); toast.success(on ? "自动发布已开启" : "自动发布已关闭"); await load() }
    catch (e) { toast.error((e as Error).message) }
  }

  const scan = async () => {
    setScanning(true)
    try {
      const r = await api.publishScan()
      const s = r.scan as { queued?: number; dup?: number; no_title?: number }
      toast.success(`扫描完成:新入队 ${s.queued ?? 0} 条,已存在 ${s.dup ?? 0} 条` +
        (s.no_title ? `,${s.no_title} 条缺标题` : ""))
      await load()
    } catch (e) { toast.error((e as Error).message) }
    finally { setScanning(false) }
  }

  const bind = async (dir: string) => {
    const fid = bindDir[dir]
    if (!fid) return
    try {
      await api.publishSaveMappings({ [dir]: fid })
      toast.success("已绑定,文件已收进队列")
      await load()
    } catch (e) { toast.error((e as Error).message) }
  }

  const nameOf = (fid: string) => finders.find((f) => f.finder_id === fid)?.name || fid.slice(-8)
  const totalToday = Object.values(st?.today_success || {}).reduce((a, b) => a + b, 0)

  return (
    <div className="space-y-4">
      {/* 概览 */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <Send className="h-4 w-4 text-primary" />自动发作品
                {st?.enabled
                  ? <Badge variant="success" className="font-normal">已开启</Badge>
                  : <Badge variant="secondary" className="font-normal">已关闭</Badge>}
              </CardTitle>
              <CardDescription className="mt-1">
                视频丢进 inbox 里对应视频号的子目录,系统自动认领归属、取一条预设标题、按策略发布。
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">{st?.enabled ? "运行中" : "已暂停"}</span>
              <Switch checked={!!st?.enabled} onCheckedChange={toggle} />
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 rounded-md bg-muted/50 px-3 py-2 text-xs">
            <FolderOpen className="h-3.5 w-3.5 text-muted-foreground" />
            <span className="text-muted-foreground">作品目录</span>
            <code className="break-all font-mono">{st?.inbox_dir || "-"}</code>
            <span className="text-muted-foreground">(从其他电脑访问:\\Sphgj\sphgj\data\publish_inbox)</span>
            <Button size="sm" variant="outline" className="ml-auto h-7 gap-1 px-2 text-xs" onClick={scan} disabled={scanning}>
              <RefreshCw className={`h-3 w-3 ${scanning ? "animate-spin" : ""}`} />立即扫描
            </Button>
          </div>

          <div className="grid gap-2 sm:grid-cols-4">
            <Stat icon={<Clock className="h-3.5 w-3.5" />} label="待发布" value={st?.pending ?? 0} />
            <Stat icon={<Send className="h-3.5 w-3.5" />} label="发布中" value={st?.running ?? 0} />
            <Stat icon={<CheckCircle2 className="h-3.5 w-3.5" />} label="今日已发" value={totalToday} />
            <Stat icon={<AlertTriangle className="h-3.5 w-3.5" />} label="待核对" value={st?.need_review ?? 0} warn={(st?.need_review ?? 0) > 0} />
          </div>

          {!st?.in_window && st?.enabled && (
            <p className="text-xs text-warning-foreground">
              当前不在发布时间窗内({String(st?.cfg?.window_start)}–{String(st?.cfg?.window_end)}),自动发布会在窗口内执行。
            </p>
          )}

          {/* 未认领目录 */}
          {!!st?.unbound?.length && (
            <div className="space-y-2 rounded-md border border-warning/50 bg-warning/10 p-3">
              <p className="flex items-center gap-1.5 text-xs font-medium">
                <AlertTriangle className="h-3.5 w-3.5" />
                有 {st.unbound.length} 个目录认不出归属,里面的视频不会被发布
              </p>
              {st.unbound.map((u) => (
                <div key={u.dir} className="flex flex-wrap items-center gap-2 text-xs">
                  <code className="rounded bg-background px-1.5 py-0.5 font-mono">{u.dir}</code>
                  <span className="text-muted-foreground">{u.files} 个视频 · {u.reason === "ambiguous" ? "匹配到多个视频号" : "没找到对应视频号"}</span>
                  <Select value={bindDir[u.dir] || ""} onValueChange={(v) => setBindDir({ ...bindDir, [u.dir]: v })}>
                    <SelectTrigger className="h-7 w-48 text-xs"><SelectValue placeholder="选择视频号…" /></SelectTrigger>
                    <SelectContent>
                      {finders.map((f) => <SelectItem key={f.finder_id} value={f.finder_id}>{f.name}</SelectItem>)}
                    </SelectContent>
                  </Select>
                  <Button size="sm" variant="outline" className="h-7 px-2 text-xs"
                    disabled={!bindDir[u.dir]} onClick={() => bind(u.dir)}>绑定</Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 账号选项卡片:同款页签胶囊,选中哪个账号下面表格就只看哪个 */}
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

      <TaskTable mode="queue" finderId={finder === "__all__" ? "" : finder} />
    </div>
  )
}

function Stat({ icon, label, value, warn }: { icon: React.ReactNode; label: string; value: number; warn?: boolean }) {
  return (
    <div className={`rounded-md border px-3 py-2 ${warn ? "border-warning/60 bg-warning/10" : ""}`}>
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">{icon}{label}</div>
      <p className="mt-0.5 text-lg font-semibold tabular-nums">{value}</p>
    </div>
  )
}
