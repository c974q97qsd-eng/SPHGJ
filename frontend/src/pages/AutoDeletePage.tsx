import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Badge } from "@/components/ui/badge"
import { Label } from "@/components/ui/label"
import { LoadingState, ErrorState, EmptyState } from "@/components/common/states"
import { api, type AutoDeleteConfig, type DeleteLogItem } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { toast } from "sonner"
import { Trash2, Save, Loader2, History, Search, Eraser } from "lucide-react"

export function AutoDeletePage() {
  const [cfg, setCfg] = useState<AutoDeleteConfig | null>(null)
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const c = await api.getAutoDelete()
      setCfg(c); setText((c.keywords || []).join("\n")); setError(null)
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  if (loading) return <LoadingState />
  if (error || !cfg) return <ErrorState message={error || "加载失败"} onRetry={load} />

  const keywords = text.split("\n").map((s) => s.trim()).filter(Boolean)

  const save = async () => {
    if (cfg.enabled && !keywords.length) { toast.error("启用自动删除需至少一个关键字"); return }
    setSaving(true)
    try {
      await api.setAutoDelete({ enabled: cfg.enabled, keywords })
      setCfg({ enabled: cfg.enabled, keywords }); setText(keywords.join("\n"))
      toast.success("已保存")
    } catch (e) { toast.error((e as Error).message) } finally { setSaving(false) }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Trash2 className="h-5 w-5 text-primary" />关键字自动删除</CardTitle>
          <CardDescription>抓到新评论命中关键字时自动删除(同一评论只删一次)。一级/二级评论均生效,对所有账号生效。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 启用开关 */}
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div>
              <Label className="text-sm font-medium">启用自动删除</Label>
              <p className="text-xs text-muted-foreground">关闭后新评论不会自动删除</p>
            </div>
            <Switch checked={cfg.enabled} onCheckedChange={(v) => setCfg({ ...cfg, enabled: v })} aria-label="启用自动删除" />
          </div>

          {/* 关键字编辑(一行一个) */}
          <div className="space-y-2">
            <Label className="text-sm font-medium">关键字(一行一个)</Label>
            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"每行一个关键字,例如:\n广告\n加微信\n私聊"}
              className="min-h-[160px] font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">评论内容包含关键字即删除(子串匹配)。当前 {keywords.length} 个关键字。</p>
          </div>

          <div className="flex justify-end pt-2">
            <Button onClick={save} disabled={saving} className="gap-1.5">
              {saving ? <Loader2 className="animate-spin" /> : <Save className="h-3.5 w-3.5" />}保存
            </Button>
          </div>
        </CardContent>
      </Card>

      <DeleteLogsCard />
    </div>
  )
}

/** 删除记录卡片:展示关键字命中删除的评论记录(数据库持久化),支持搜索/清空/加载更多。 */
function DeleteLogsCard() {
  const LIMIT = 50
  const [logs, setLogs] = useState<DeleteLogItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [qInput, setQInput] = useState("")
  const [q, setQ] = useState("")
  const [moreLoading, setMoreLoading] = useState(false)

  const load = async (query: string) => {
    setLoading(true)
    try {
      const r = await api.getDeleteLogs({ q: query || undefined, limit: LIMIT, offset: 0 })
      setLogs(r.items); setTotal(r.total)
    } catch { /* ignore */ } finally { setLoading(false) }
  }
  useEffect(() => { load("") }, [])

  const search = () => { setQ(qInput); load(qInput) }

  const loadMore = async () => {
    setMoreLoading(true)
    try {
      const r = await api.getDeleteLogs({ q: q || undefined, limit: LIMIT, offset: logs.length })
      setLogs((prev) => [...prev, ...r.items])
    } catch { /* ignore */ } finally { setMoreLoading(false) }
  }

  const clear = async () => {
    try {
      await api.clearDeleteLogs()
      toast.success("已清空删除记录")
      setQInput(""); setQ(""); load("")
    } catch (e) { toast.error("清空失败:" + (e as Error).message) }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><History className="h-5 w-5 text-primary" />删除记录</CardTitle>
        <CardDescription>自动删除命中关键字的评论记录(共 {total} 条,保存在数据库)</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* 搜索 + 清空 */}
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && search()}
              placeholder="搜索内容/昵称/关键字"
              className="pl-8"
              aria-label="搜索删除记录"
            />
          </div>
          <Button variant="outline" size="sm" onClick={clear} className="gap-1.5" aria-label="清空删除记录">
            <Eraser className="h-3.5 w-3.5" />清空
          </Button>
        </div>

        {loading ? (
          <LoadingState />
        ) : logs.length === 0 ? (
          <EmptyState icon={<Trash2 className="h-6 w-6" />} title="暂无删除记录"
            description={q ? "没有匹配的记录" : "启用自动删除后,命中关键字的评论会记录在这里"} />
        ) : (
          <div className="space-y-2">
            <div className="max-h-[480px] space-y-2 overflow-y-auto pr-1">
              {logs.map((log) => (
                <div key={log.comment_id + log.deleted_at} className="rounded-md border p-2.5 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="truncate font-medium">{log.nickname || "匿名"}</span>
                      <Badge variant="secondary" className="shrink-0 font-normal">{log.account_name}</Badge>
                    </div>
                    <span className="shrink-0 text-[10px] text-muted-foreground">{fmtTime(log.deleted_at)}</span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-muted-foreground">{log.content}</p>
                  <div className="mt-1.5">
                    <Badge variant="destructive" className="font-normal">命中: {log.keyword}</Badge>
                  </div>
                </div>
              ))}
            </div>
            {logs.length < total && (
              <div className="flex justify-center pt-1">
                <Button variant="outline" size="sm" onClick={loadMore} disabled={moreLoading} className="gap-1.5">
                  {moreLoading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}加载更多
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
