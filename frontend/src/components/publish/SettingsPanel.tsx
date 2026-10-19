import { useCallback, useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Separator } from "@/components/ui/separator"
import { LoadingState, ErrorState } from "@/components/common/states"
import { api, type PublishFinder, type PublishStatus } from "@/lib/api"
import { toast } from "sonner"
import { Save, Settings2, ShieldCheck } from "lucide-react"

/** 发布策略:全局节流参数 + 每个视频号的今日进度。 */
export function SettingsPanel() {
  const [st, setSt] = useState<PublishStatus | null>(null)
  const [finders, setFinders] = useState<PublishFinder[]>([])
  const [form, setForm] = useState<Record<string, string | number | boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [s, f] = await Promise.all([api.publishStatus(), api.publishFinders()])
      setSt(s)
      setFinders(f.items)
      const c = (s.cfg || {}) as Record<string, string | number | boolean>
      setForm({
        enabled: !!c.enabled,
        auto_start_account: !!c.auto_start_account,
        window_start: String(c.window_start ?? "08:00"),
        window_end: String(c.window_end ?? "22:30"),
        daily_limit: Number(c.daily_limit ?? 1),
        min_gap_sec: Number(c.min_gap_sec ?? 2400),
        gap_jitter_sec: Number(c.gap_jitter_sec ?? 3000),
        min_login_remain_sec: Number(c.min_login_remain_sec ?? 7200),
        scan_interval_sec: Number(c.scan_interval_sec ?? 60),
        inbox_dir: String(c.inbox_dir ?? ""),
      })
      setError(null)
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const save = async () => {
    setSaving(true)
    try {
      await api.publishSaveSettings({
        ...form,
        daily_limit: Number(form.daily_limit) || 0,
        min_gap_sec: Number(form.min_gap_sec) || 0,
        gap_jitter_sec: Number(form.gap_jitter_sec) || 0,
        min_login_remain_sec: Number(form.min_login_remain_sec) || 0,
        scan_interval_sec: Number(form.scan_interval_sec) || 60,
      })
      toast.success("策略已保存")
      await load()
    } catch (e) { toast.error((e as Error).message) } finally { setSaving(false) }
  }

  const set = (k: string, v: string | number | boolean) => setForm({ ...form, [k]: v })
  const minGap = Number(form.min_gap_sec) || 0
  const jitter = Number(form.gap_jitter_sec) || 0

  if (loading) return <LoadingState />
  if (error || !st) return <ErrorState message={error || "加载失败"} onRetry={load} />

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <Settings2 className="h-4 w-4 text-primary" />发布策略
          </CardTitle>
          <CardDescription>
            发布是写操作,比抓取更容易触发风控 —— 这些阀门宁可保守。修改后立即生效。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Row label="总开关" hint="关闭后只扫描入队,不会自动发布(手动「立即发」仍可用)">
            <Switch checked={!!form.enabled} onCheckedChange={(v) => set("enabled", v)} />
          </Row>
          <Row label="账号未运行时自动拉起" hint="关闭则只发正在运行的账号,其余排队等待">
            <Switch checked={!!form.auto_start_account} onCheckedChange={(v) => set("auto_start_account", v)} />
          </Row>
          <Separator />
          <Row label="发布时间窗" hint="窗外不发(支持跨午夜,如 22:00–02:00)">
            <div className="flex items-center gap-2">
              <Input className="h-8 w-24 text-sm" value={String(form.window_start)} onChange={(e) => set("window_start", e.target.value)} />
              <span className="text-xs text-muted-foreground">至</span>
              <Input className="h-8 w-24 text-sm" value={String(form.window_end)} onChange={(e) => set("window_end", e.target.value)} />
            </div>
          </Row>
          <Row label="单号每日上限" hint="每个视频号每天最多发几条(0 = 不限,不建议)">
            <Input type="number" min={0} max={10} className="h-8 w-24 text-sm"
              value={String(form.daily_limit)} onChange={(e) => set("daily_limit", e.target.value)} />
          </Row>
          <Row label="发布间隔" hint={`同一视频号两次发布至少间隔 ${Math.round(minGap / 60)} 分钟,再叠加 0–${Math.round(jitter / 60)} 分钟随机抖动(实际约 ${Math.round(minGap / 60)}–${Math.round((minGap + jitter) / 60)} 分钟)`}>
            <div className="flex items-center gap-1">
              <Input type="number" min={0} className="h-8 w-20 text-sm"
                value={String(Math.round(minGap / 60))}
                onChange={(e) => set("min_gap_sec", Number(e.target.value) * 60)} />
              <span className="text-xs text-muted-foreground">分钟</span>
            </div>
          </Row>
          <Row label="登录态剩余门槛" hint="剩余有效期低于该值不发,避免发到一半掉登录被迫重新扫码">
            <div className="flex items-center gap-1">
              <Input type="number" min={0} className="h-8 w-20 text-sm"
                value={String(Math.round((Number(form.min_login_remain_sec) || 0) / 60))}
                onChange={(e) => set("min_login_remain_sec", Number(e.target.value) * 60)} />
              <span className="text-xs text-muted-foreground">分钟</span>
            </div>
          </Row>
          <Row label="扫描间隔" hint="多久扫一次 inbox 目录找新视频">
            <div className="flex items-center gap-1">
              <Input type="number" min={10} className="h-8 w-20 text-sm"
                value={String(form.scan_interval_sec)} onChange={(e) => set("scan_interval_sec", e.target.value)} />
              <span className="text-xs text-muted-foreground">秒</span>
            </div>
          </Row>
          <Row label="作品目录" hint="留空 = 程序目录下的 data/publish_inbox(建议留空)">
            <Input className="h-8 w-80 text-xs font-mono" placeholder="data/publish_inbox"
              value={String(form.inbox_dir)} onChange={(e) => set("inbox_dir", e.target.value)} />
          </Row>
          <div className="pt-1">
            <Button className="gap-1" onClick={save} disabled={saving}><Save className="h-4 w-4" />保存策略</Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4 text-primary" />各号进度
          </CardTitle>
          <CardDescription>今日已发 / 标题余量 / 连败状态</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {finders.map((f) => (
            <div key={f.finder_id} className="rounded-md border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium" title={f.name}>{f.name}</span>
                <div className="flex shrink-0 items-center gap-1">
                  {f.human_stopped && <Badge variant="secondary" className="text-[10px] font-normal">手动停</Badge>}
                  {f.fail_streak >= 2 && <Badge variant="destructive" className="text-[10px] font-normal">已熔断</Badge>}
                  {!f.logged_in && <Badge variant="warning" className="text-[10px] font-normal">未登录</Badge>}
                </div>
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                今日 {f.today_success}/{Number(form.daily_limit)} 条 · 标题剩 {f.titles_unused}
                {f.last_success_at && ` · 上次 ${f.last_success_at.replace("T", " ").slice(5, 16)}`}
              </p>
            </div>
          ))}
          {!finders.length && <p className="text-xs text-muted-foreground">还没有带视频号 ID 的账号</p>}
        </CardContent>
      </Card>
    </div>
  )
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="min-w-0">
        <Label className="text-sm">{label}</Label>
        {hint && <p className="mt-0.5 max-w-xl text-xs text-muted-foreground">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}
