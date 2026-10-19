import { useCallback, useEffect, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { LoadingState, ErrorState, EmptyState } from "@/components/common/states"
import { api, type PublishFinder, type PublishTitle } from "@/lib/api"
import { toast } from "sonner"
import { Plus, Trash2, ListChecks, Eraser } from "lucide-react"

/** 标题池:一条视频配一条标题,用过即作废(用完即弃)。 */
export function TitlesPanel() {
  const [pool, setPool] = useState("")           // "" = 通用池
  const [items, setItems] = useState<PublishTitle[]>([])
  const [finders, setFinders] = useState<PublishFinder[]>([])
  const [generic, setGeneric] = useState<{ total: number; unused: number }>({ total: 0, unused: 0 })
  const [draft, setDraft] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async (p: string) => {
    setLoading(true)
    try {
      const [r, f] = await Promise.all([api.publishTitles(p), api.publishFinders()])
      setItems(r.items)
      setFinders(f.items)
      setGeneric(f.generic_titles)
      setError(null)
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }, [])

  useEffect(() => { load(pool) }, [load, pool])

  const add = async () => {
    const lines = draft.split("\n").map((x) => x.trim()).filter(Boolean)
    if (!lines.length) { toast.error("先粘贴标题,一行一条"); return }
    setSaving(true)
    try {
      const r = await api.publishAddTitles(pool, draft)
      toast.success(`添加 ${r.added} 条` + (r.added < lines.length ? `,${lines.length - r.added} 条重复已跳过` : "") +
        (r.titles_filled ? `,已给 ${r.titles_filled} 条待发任务补上标题` : ""))
      setDraft("")
      await load(pool)
    } catch (e) { toast.error((e as Error).message) } finally { setSaving(false) }
  }

  const clearUsed = async () => {
    try {
      const r = await api.publishClearTitles(pool, true)
      toast.success(`清掉 ${r.deleted} 条已用标题`)
      await load(pool)
    } catch (e) { toast.error((e as Error).message) }
  }

  const unused = items.filter((t) => !t.used_count).length
  const curFinder = finders.find((f) => f.finder_id === pool)

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ListChecks className="h-4 w-4 text-primary" />标题池
          </CardTitle>
          <CardDescription>
            一条视频消耗一条标题,用过即作废、不会重复使用。专属池优先,用完后自动回落到通用池。
            话题直接写在标题里(如 <code className="font-mono">#男鞋</code>),发布时会一并写入描述。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Select value={pool || "__generic__"} onValueChange={(v) => setPool(v === "__generic__" ? "" : v)}>
              <SelectTrigger className="h-8 w-56 text-xs"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__generic__">通用池(所有视频号兜底) · 剩 {generic.unused}/{generic.total}</SelectItem>
                {finders.map((f) => (
                  <SelectItem key={f.finder_id} value={f.finder_id}>
                    {f.name} · 剩 {f.titles_unused}/{f.titles_total}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Badge variant={unused > 0 ? "success" : "destructive"} className="font-normal">
              未用 {unused} / 共 {items.length}
            </Badge>
            {curFinder && curFinder.today_success > 0 && (
              <span className="text-xs text-muted-foreground">今日已发 {curFinder.today_success} 条</span>
            )}
            <Button size="sm" variant="outline" className="ml-auto h-8 gap-1 px-2 text-xs" onClick={clearUsed}>
              <Eraser className="h-3.5 w-3.5" />清掉已用
            </Button>
          </div>

          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={"一行一条标题,可批量粘贴。例如:\n秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\n这双鞋的配色比图片还好看 #穿搭"}
              className="min-h-[92px] text-sm"
            />
            <Button className="h-auto gap-1 sm:w-28" onClick={add} disabled={saving}>
              <Plus className="h-4 w-4" />添加
            </Button>
          </div>
        </CardContent>
      </Card>

      {loading ? <LoadingState />
        : error ? <ErrorState message={error} onRetry={() => load(pool)} />
        : !items.length ? <EmptyState icon={<ListChecks className="h-5 w-5" />} title="标题池是空的"
            description="先在上面粘贴标题。没有标题的视频不会发布,会一直排队等标题" />
        : (
          <div className="divide-y rounded-md border">
            {items.map((t) => (
              <div key={t.id} className="flex items-start gap-3 px-3 py-2 text-sm hover:bg-muted/30">
                <Badge variant={t.used_count ? "secondary" : "success"} className="mt-0.5 shrink-0 font-normal">
                  {t.used_count ? "已用" : "未用"}
                </Badge>
                <div className="min-w-0 flex-1">
                  <p className="break-words">{t.content}</p>
                  {t.used_count > 0 && t.last_used_at && (
                    <p className="text-xs text-muted-foreground">用于任务 #{t.used_task_id} · {t.last_used_at.replace("T", " ").slice(5, 16)}</p>
                  )}
                </div>
                <Button size="sm" variant="ghost" className="h-7 px-1" title="删除"
                  onClick={async () => {
                    try { await api.publishDeleteTitle(t.id); await load(pool) }
                    catch (e) { toast.error((e as Error).message) }
                  }}><Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
            ))}
          </div>
        )}
    </div>
  )
}
