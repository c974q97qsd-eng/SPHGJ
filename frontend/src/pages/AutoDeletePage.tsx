import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { LoadingState, ErrorState } from "@/components/common/states"
import { api, type AutoDeleteConfig } from "@/lib/api"
import { toast } from "sonner"
import { Trash2, Save, Loader2 } from "lucide-react"

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
    </div>
  )
}
