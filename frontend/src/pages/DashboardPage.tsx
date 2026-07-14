import { useMemo } from "react"
import { Link } from "react-router-dom"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { StatusBadge } from "@/components/common/status-badge"
import { EmptyState } from "@/components/common/states"
import { useAccounts } from "@/hooks/useAccounts"
import { useComments } from "@/hooks/useComments"
import { fmtTime } from "@/lib/utils"
import { Plus, Users, MessageSquare, Reply, Activity, ArrowRight } from "lucide-react"

export function DashboardPage() {
  const { accounts, loading } = useAccounts()
  const { items, total } = useComments({ limit: 5 })

  const stats = useMemo(() => {
    const online = accounts.filter((a) => a.logged_in).length
    const replied = accounts.reduce((s, a) => s + a.replied, 0)
    const newC = accounts.reduce((s, a) => s + a.new_comments, 0)
    return { online, total: accounts.length, replied, newC }
  }, [accounts])

  if (!loading && accounts.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={<Users className="h-6 w-6" />}
          title="欢迎使用视频号评论区管理"
          description="扫码添加第一个账号开始管理评论区:自动评论、关键字回复、集中查看与手动回复"
          action={<Button asChild className="gap-1.5"><Link to="/accounts"><Plus className="h-4 w-4" />添加账号</Link></Button>}
        />
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {/* 概览卡片 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard icon={Users} label="账号" value={`${stats.online}/${stats.total}`} hint="在线/总数" />
        <StatCard icon={MessageSquare} label="评论总数" value={total} hint="所有账号" />
        <StatCard icon={Reply} label="已回复" value={stats.replied} hint="累计" />
        <StatCard icon={Activity} label="本次新增" value={stats.newC} hint="启动后" accent />
      </div>

      {/* 账号状态 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <span className="flex items-center gap-2"><Users className="h-4 w-4 text-primary" />账号状态</span>
            <Button asChild variant="ghost" size="sm" className="gap-1 text-xs"><Link to="/accounts">管理<ArrowRight className="h-3 w-3" /></Link></Button>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {accounts.map((a) => (
              <div key={a.id} className="flex items-center justify-between rounded-lg border p-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{a.name}</p>
                  <p className="text-xs text-muted-foreground">{a.total_comments} 评论 · 新增 {a.new_comments}</p>
                </div>
                {a.running ? <StatusBadge state="running" /> : a.logged_in ? <StatusBadge state="online" /> : <StatusBadge state="idle" />}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* 最近评论 */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center justify-between text-base">
            <span className="flex items-center gap-2"><MessageSquare className="h-4 w-4 text-primary" />最近评论</span>
            <Button asChild variant="ghost" size="sm" className="gap-1 text-xs"><Link to="/comments">查看全部<ArrowRight className="h-3 w-3" /></Link></Button>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {items.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">暂无评论,启动引擎后将自动抓取</p>
          ) : (
            <div className="space-y-2">
              {items.slice(0, 5).map((c) => (
                <div key={c.comment_id} className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-muted/40">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">{c.content}</p>
                    <p className="text-xs text-muted-foreground">{c.nickname} · {fmtTime(c.create_time)}</p>
                  </div>
                  {c.replied ? <Badge variant="success" className="font-normal">已回</Badge> : <Badge variant="secondary" className="font-normal">未回</Badge>}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, hint, accent }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string | number; hint: string; accent?: boolean }) {
  return (
    <Card className="animate-slide-up">
      <CardContent className="flex items-center gap-3 p-4">
        <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${accent ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground"}`}>
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <div className="text-2xl font-semibold leading-none">{value}</div>
          <div className="mt-1 text-xs text-muted-foreground">{label} · {hint}</div>
        </div>
      </CardContent>
    </Card>
  )
}
