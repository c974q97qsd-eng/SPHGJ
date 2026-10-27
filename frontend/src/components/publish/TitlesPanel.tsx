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
  // 粘贴预览:后端清理后的真实结果(序号/全角空格已去掉,引号和行尾标点保留)
  const [preview, setPreview] = useState<{ items: string[]; cleaned: number; dropped: number } | null>(null)

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

  // 粘贴/输入后 350ms 问一次后端清理结果(规则只在后端一份,避免两边不一致)
  useEffect(() => {
    const text = draft
    if (!text.trim()) { setPreview(null); return }
    const t = setTimeout(() => {
      api.publishPreviewTitles(pool, text)
        .then((r) => setPreview({ items: r.items, cleaned: r.cleaned, dropped: r.dropped }))
        .catch(() => setPreview(null))
    }, 350)
    return () => clearTimeout(t)
  }, [draft, pool])

  const add = async () => {
    if (!draft.trim()) { toast.error("先粘贴标题"); return }
    setSaving(true)
    try {
      const r = await api.publishAddTitles(pool, draft)
      const bits = [`添加 ${r.added} 条`]
      if (r.cleaned) bits.push(`已自动清理 ${r.cleaned} 条的序号等格式`)
      if (r.dropped) bits.push(`跳过 ${r.dropped} 条空行/重复`)
      if (r.added < (r.parsed ?? r.added)) bits.push(`池内已有 ${(r.parsed ?? r.added) - r.added} 条相同标题`)
      if (r.titles_filled) bits.push(`已给 ${r.titles_filled} 条待发任务补上标题`)
      toast.success(bits.join(","))
      setDraft("")
      setPreview(null)
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
              placeholder={"直接粘贴即可,行首序号、全角空格会自动清掉,引号和行尾标点保留。例如:\n1. 秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\n2. 这双鞋的配色比图片还好看 #穿搭"}
              className="min-h-[92px] text-sm"
            />
            <Button className="h-auto gap-1 sm:w-28" onClick={add} disabled={saving}>
              <Plus className="h-4 w-4" />添加
            </Button>
          </div>

          {/* 粘贴预览:让用户加之前就看到清理结果,不用赌 */}
          {preview && (
            <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-medium">识别到 {preview.items.length} 条标题</span>
                {preview.cleaned > 0 && <span className="text-muted-foreground">· 已自动清理 {preview.cleaned} 条的序号/全角空格</span>}
                {preview.dropped > 0 && <span className="text-muted-foreground">· 跳过 {preview.dropped} 条空行/重复</span>}
                {!preview.items.length && <span className="text-destructive">· 没有有效内容</span>}
              </div>
              {preview.items.length > 0 && (
                <ol className="mt-1.5 max-h-32 space-y-0.5 overflow-y-auto text-muted-foreground">
                  {preview.items.map((t, i) => (
                    <li key={i} className="truncate">{i + 1}. {t}</li>
                  ))}
                </ol>
              )}
            </div>
          )}
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
