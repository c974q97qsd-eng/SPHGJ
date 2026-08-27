import { useEffect, useState } from "react"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { ScrollArea } from "@/components/ui/scroll-area"
import { ChevronUp, ChevronDown } from "lucide-react"
import { api, type MetricDef } from "@/lib/api"
import { toast } from "sonner"

const MAX = 9

/** 直播大屏卡片指标自定义:左选指标(按组) + 右排序/移除,保存到后端 config.card_fields。 */
export function CardFieldsSettings({ open, onOpenChange, metricDict, cardFields, onSaved }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  metricDict: MetricDef[]
  cardFields: string[]
  onSaved: () => void
}) {
  const [selected, setSelected] = useState<string[]>(cardFields)
  useEffect(() => { if (open) setSelected(cardFields) }, [open, cardFields])

  const toggle = (key: string) => {
    setSelected((prev) => prev.includes(key) ? prev.filter((k) => k !== key) : prev.length < MAX ? [...prev, key] : prev)
  }
  const move = (i: number, dir: -1 | 1) => {
    setSelected((prev) => {
      const j = i + dir
      if (j < 0 || j >= prev.length) return prev
      const next = [...prev]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
  }
  const save = async () => {
    if (selected.length !== MAX) { toast.error(`需选满 ${MAX} 个`); return }
    try {
      await api.patchConfig({ card_fields: selected })
      toast.success("卡片配置已保存")
      onSaved()
      onOpenChange(false)
    } catch (e) { toast.error("保存失败:" + (e as Error).message) }
  }

  const groups = [...new Set(metricDict.map((m) => m.group))]
  const nameOf = (k: string) => metricDict.find((m) => m.key === k)?.display_name ?? k

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>自定义卡片指标(选 {MAX} 项,顺序即显示顺序)</DialogTitle>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-4">
          <ScrollArea className="h-[420px] pr-3">
            <div className="space-y-3">
              {groups.map((g) => (
                <div key={g}>
                  <div className="mb-1 text-xs font-semibold text-muted-foreground">{g}</div>
                  <div className="space-y-1">
                    {metricDict.filter((m) => m.group === g).map((m) => (
                      <label key={m.key} className="flex cursor-pointer items-center gap-2 text-sm">
                        <Checkbox checked={selected.includes(m.key)} onChange={() => toggle(m.key)}
                          disabled={!selected.includes(m.key) && selected.length >= MAX} />
                        <span>{m.display_name}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </ScrollArea>
          <div>
            <div className="mb-1 text-xs font-semibold text-muted-foreground">已选({selected.length}/{MAX})</div>
            <ScrollArea className="h-[420px]">
              <div className="space-y-1">
                {selected.map((k, i) => (
                  <div key={k} className="flex items-center justify-between rounded border px-2 py-1 text-sm">
                    <span className="truncate">{nameOf(k)}</span>
                    <span className="flex shrink-0 gap-0.5">
                      <Button size="icon" variant="ghost" className="h-6 w-6" onClick={() => move(i, -1)} disabled={i === 0}><ChevronUp className="h-3 w-3" /></Button>
                      <Button size="icon" variant="ghost" className="h-6 w-6" onClick={() => move(i, 1)} disabled={i === selected.length - 1}><ChevronDown className="h-3 w-3" /></Button>
                      <Button size="icon" variant="ghost" className="h-6 w-6" onClick={() => toggle(k)}>✕</Button>
                    </span>
                  </div>
                ))}
                {selected.length === 0 && <div className="text-xs text-muted-foreground">从左侧选择指标</div>}
              </div>
            </ScrollArea>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={save} disabled={selected.length !== MAX}>保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
