import { useCallback } from "react"
import { useSearchParams } from "react-router-dom"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { CommentListPanel } from "@/components/comments/CommentListPanel"
import { AutoReplyPanel } from "@/components/comments/AutoReplyPanel"
import { AutoDeletePanel } from "@/components/comments/AutoDeletePanel"
import { MessageSquare, Reply, Trash2 } from "lucide-react"

/** 三个页签:评论列表(默认) / 自动回复 / 自动删除。
 *  页签写入 URL query(?tab=reply),刷新或分享链接后仍停在同一页签;
 *  旧路由 /auto-reply、/auto-delete 会重定向到对应页签。 */
const TABS = [
  { value: "list", label: "评论列表", icon: MessageSquare, hint: "实时抓取的评论,可手动回复 / 删除 / 置顶" },
  { value: "reply", label: "自动回复", icon: Reply, hint: "按关键字自动回复新评论" },
  { value: "delete", label: "自动删除", icon: Trash2, hint: "按关键字自动删除新评论,并保留删除记录" },
] as const

type TabValue = (typeof TABS)[number]["value"]

export function CommentsPage() {
  const [params, setParams] = useSearchParams()
  const raw = params.get("tab")
  const tab: TabValue = (TABS.some((t) => t.value === raw) ? raw : "list") as TabValue

  const onTabChange = useCallback((v: string) => {
    const next = new URLSearchParams(params)
    if (v === "list") next.delete("tab"); else next.set("tab", v)
    setParams(next, { replace: true })
  }, [params, setParams])

  const hint = TABS.find((t) => t.value === tab)?.hint

  return (
    <Tabs value={tab} onValueChange={onTabChange} className="space-y-3">
      {/* 页签栏:左页签、右说明,窄屏自动换行 */}
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

      <TabsContent value="list" className="mt-0">
        <CommentListPanel />
      </TabsContent>
      <TabsContent value="reply" className="mt-0">
        <AutoReplyPanel />
      </TabsContent>
      <TabsContent value="delete" className="mt-0">
        <AutoDeletePanel />
      </TabsContent>
    </Tabs>
  )
}
