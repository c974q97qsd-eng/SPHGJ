import { useCallback } from "react"
import { useSearchParams } from "react-router-dom"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { QueuePanel } from "@/components/publish/QueuePanel"
import { TitlesPanel } from "@/components/publish/TitlesPanel"
import { SettingsPanel } from "@/components/publish/SettingsPanel"
import { HistoryPanel } from "@/components/publish/HistoryPanel"
import { Send, ListChecks, Settings2, History } from "lucide-react"

/** 发布管理:待发布 / 标题池 / 发布策略 / 发布记录。页签写入 URL,刷新后停在原处。 */
const TABS = [
  { value: "queue", label: "待发布", icon: Send, hint: "inbox 扫描进来的视频,按策略排队等发" },
  { value: "titles", label: "标题池", icon: ListChecks, hint: "预设标题,一条视频用一条,用完即弃" },
  { value: "settings", label: "发布策略", icon: Settings2, hint: "日限、间隔、时间窗、自动拉起账号" },
  { value: "history", label: "发布记录", icon: History, hint: "成功与失败留档,失败可核对原因后重试" },
] as const

type TabValue = (typeof TABS)[number]["value"]

export function PublishPage() {
  const [params, setParams] = useSearchParams()
  const raw = params.get("tab")
  const tab: TabValue = (TABS.some((t) => t.value === raw) ? raw : "queue") as TabValue

  const onTabChange = useCallback((v: string) => {
    const next = new URLSearchParams(params)
    if (v === "queue") next.delete("tab"); else next.set("tab", v)
    setParams(next, { replace: true })
  }, [params, setParams])

  const hint = TABS.find((t) => t.value === tab)?.hint

  return (
    <Tabs value={tab} onValueChange={onTabChange} className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <TabsList>
          {TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="gap-1.5">
              <t.icon className="h-3.5 w-3.5" />
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>

      <TabsContent value="queue" className="mt-0"><QueuePanel /></TabsContent>
      <TabsContent value="titles" className="mt-0"><TitlesPanel /></TabsContent>
      <TabsContent value="settings" className="mt-0"><SettingsPanel /></TabsContent>
      <TabsContent value="history" className="mt-0"><HistoryPanel /></TabsContent>
    </Tabs>
  )
}
