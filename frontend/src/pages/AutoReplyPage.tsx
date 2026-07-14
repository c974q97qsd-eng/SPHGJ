import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { LoadingState, ErrorState } from "@/components/common/states"
import { api, type AutoReplyConfig } from "@/lib/api"
import { toast } from "sonner"
import { Plus, Trash2, Save, Reply, Loader2 } from "lucide-react"

export function AutoReplyPage() {
  const [cfg, setCfg] = useState<AutoReplyConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try { setCfg(await api.getAutoReply()); setError(null) } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  if (loading) return <LoadingState />
  if (error || !cfg) return <ErrorState message={error || "加载失败"} onRetry={load} />

  const update = (patch: Partial<AutoReplyConfig>) => setCfg({ ...cfg, ...patch })
  const setRule = (i: number, patch: Partial<{ keyword: string; reply: string }>) =>
    update({ rules: cfg.rules.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const addRule = () => update({ rules: [...cfg.rules, { keyword: "", reply: "" }] })
  const delRule = (i: number) => update({ rules: cfg.rules.filter((_, j) => j !== i) })

  const save = async () => {
    const clean = cfg.rules.filter((r) => r.keyword.trim())
    if (cfg.enabled && !clean.length) { toast.error("启用自动回复需至少一条规则"); return }
    setSaving(true)
    try { await api.setAutoReply({ enabled: cfg.enabled, rules: clean }); setCfg({ enabled: cfg.enabled, rules: clean }); toast.success("已保存") }
    catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  return (
    <div className="max-w-3xl space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Reply className="h-5 w-5 text-primary" />关键字自动回复</CardTitle>
          <CardDescription>抓到新评论命中关键字时自动回复(同一评论只回一次)。规则对所有账号生效。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* 启用开关 */}
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div>
              <Label className="text-sm font-medium">启用自动回复</Label>
              <p className="text-xs text-muted-foreground">关闭后新评论不会自动回复</p>
            </div>
            <Switch checked={cfg.enabled} onCheckedChange={(v) => update({ enabled: v })} aria-label="启用自动回复" />
          </div>

          <Separator />

          {/* 规则编辑 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label className="text-sm font-medium">回复规则</Label>
              <Button size="sm" variant="outline" onClick={addRule} className="gap-1.5"><Plus className="h-3.5 w-3.5" />添加规则</Button>
            </div>
            {cfg.rules.length === 0 ? (
              <p className="rounded-md bg-muted/40 px-3 py-6 text-center text-sm text-muted-foreground">还没有规则,点击「添加规则」</p>
            ) : (
              <div className="space-y-2">
                {cfg.rules.map((r, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <Input className="flex-1" placeholder="关键字" value={r.keyword} onChange={(e) => setRule(i, { keyword: e.target.value })} />
                    <span className="text-muted-foreground">→</span>
                    <Input className="flex-[2]" placeholder="回复内容" value={r.reply} onChange={(e) => setRule(i, { reply: e.target.value })} />
                    <Button size="icon" variant="ghost" onClick={() => delRule(i)} aria-label="删除规则"><Trash2 className="h-4 w-4 text-destructive" /></Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex justify-end pt-2">
            <Button onClick={save} disabled={saving} className="gap-1.5">
              {saving ? <Loader2 className="animate-spin" /> : <Save className="h-4 w-4" />}保存规则
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
