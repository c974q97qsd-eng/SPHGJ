import { useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { StatusBadge } from "@/components/common/status-badge"
import { EmptyState, LoadingState, ErrorState } from "@/components/common/states"
import { AddAccountDialog } from "@/components/accounts/AddAccountDialog"
import { useAccounts, useAccountActions, type AccountStatus } from "@/hooks/useAccounts"
import { api } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { toast } from "sonner"
import { Plus, Play, Square, Trash2, Save, Loader2, MessageSquarePlus, Users, ExternalLink } from "lucide-react"

export function AccountsPage() {
  const { accounts, loading, error, refresh } = useAccounts()
  const [addOpen, setAddOpen] = useState(false)

  if (loading && accounts.length === 0) return <LoadingState />
  if (error && accounts.length === 0) return <ErrorState message={error} onRetry={refresh} />

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">共 {accounts.length} 个账号 · 自动评论配置在每张卡片内</p>
        <Button onClick={() => setAddOpen(true)} className="gap-1.5"><Plus className="h-4 w-4" />添加账号</Button>
      </div>

      {accounts.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Users className="h-6 w-6" />}
            title="还没有账号"
            description="扫码添加第一个视频号账号,软件会自动抓取所需字段"
            action={<Button onClick={() => setAddOpen(true)} className="gap-1.5"><Plus className="h-4 w-4" />添加账号</Button>}
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {accounts.map((a) => <AccountCard key={a.id} acc={a} onRefresh={refresh} />)}
        </div>
      )}

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onDone={refresh} />
    </div>
  )
}

function AccountCard({ acc, onRefresh }: { acc: AccountStatus; onRefresh: () => void }) {
  const { busy, start, stop, openBrowser, remove } = useAccountActions(onRefresh)
  const [delOpen, setDelOpen] = useState(false)
  const [reloginOpen, setReloginOpen] = useState(false)
  const isBusy = busy === acc.id

  const handleStart = async () => {
    const res = await start(acc.id)
    if (!res?.logged_in) {
      toast.info("账号未登录,请重新扫码")
      setReloginOpen(true)
    }
  }

  const handleOpen = async () => {
    try {
      await openBrowser(acc.id)
      toast.success("已打开浏览器,关闭后自动恢复抓取")
    } catch (e) {
      toast.error("打开失败:" + (e as Error).message)
    }
  }

  return (
    <Card className="animate-slide-up">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2 text-base">
              {acc.name}
              {acc.running
                ? <StatusBadge state="running" />
                : acc.logged_in ? <StatusBadge state="online" /> : <StatusBadge state={acc.has_aid ? "idle" : "warning"} />}
            </CardTitle>
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              {acc.wx_name && <span>微信: {acc.wx_name}</span>}
              <span className="font-mono">ID: {acc.id}</span>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <Button size="sm" variant="outline" onClick={handleOpen} disabled={isBusy} aria-label="打开账号浏览器">{isBusy ? <Loader2 className="animate-spin" /> : <ExternalLink className="h-3.5 w-3.5" />}打开</Button>
            {acc.running
              ? <Button size="sm" variant="outline" onClick={() => stop(acc.id)} disabled={isBusy}>{isBusy ? <Loader2 className="animate-spin" /> : <Square className="h-3.5 w-3.5" />}停止</Button>
              : <Button size="sm" variant="outline" onClick={handleStart} disabled={isBusy}>{isBusy ? <Loader2 className="animate-spin" /> : <Play className="h-3.5 w-3.5" />}启动</Button>}
            <Button size="icon" variant="ghost" onClick={() => setDelOpen((v) => !v)} aria-label="删除"><Trash2 className="h-4 w-4 text-destructive" /></Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* 统计 */}
        <div className="grid grid-cols-4 gap-2 text-center">
          <Stat label="评论" value={acc.total_comments} />
          <Stat label="已回" value={acc.replied} />
          <Stat label="当日新增" value={acc.new_comments} accent />
          <Stat label="最近抓取" value={fmtTime(acc.last_scan || acc.last_fetched)} small />
        </div>

        {/* 删除确认 */}
        {delOpen && (
          <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-2">
            <span className="flex-1 text-xs text-destructive">确认删除该账号?(本地数据保留)</span>
            <Button size="sm" variant="destructive" onClick={async () => { await remove(acc.id, false); toast.success("已删除") }}>删除</Button>
            <Button size="sm" variant="ghost" onClick={() => setDelOpen(false)}>取消</Button>
          </div>
        )}

        {/* 自动评论配置(诉求2:账号管理模块内) */}
        <AutoCommentConfig acc={acc} />
        <AddAccountDialog open={reloginOpen} onOpenChange={setReloginOpen} onDone={onRefresh} reloginAccountId={acc.id} reloginAccountName={acc.name} reloginWxName={acc.wx_name} />
      </CardContent>
    </Card>
  )
}

function AutoCommentConfig({ acc }: { acc: AccountStatus }) {
  const [enabled, setEnabled] = useState(acc.auto_comment_enabled)
  const [content, setContent] = useState(acc.auto_comment_content)
  const [saving, setSaving] = useState(false)

  const save = async () => {
    if (enabled && !content.trim()) { toast.error("启用自动评论需填写内容"); return }
    setSaving(true)
    try { await api.setAutoComment(acc.id, enabled, content); toast.success("自动评论已保存") }
    catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <MessageSquarePlus className="h-4 w-4 text-primary" />
          <Label className="text-sm font-medium">自动评论(新视频自动发 + 置顶)</Label>
        </div>
        <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="启用自动评论" />
      </div>
      {enabled && (
        <Textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder="检测到新视频发布后自动发送的评论内容"
          className="text-sm"
          rows={2}
        />
      )}
      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={save} disabled={saving} className="gap-1.5">
          {saving ? <Loader2 className="animate-spin" /> : <Save className="h-3.5 w-3.5" />}保存
        </Button>
      </div>
    </div>
  )
}

function Stat({ label, value, accent, small }: { label: string; value: number | string; accent?: boolean; small?: boolean }) {
  return (
    <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <div className={`font-semibold ${small ? "text-xs" : "text-lg"} ${accent ? "text-primary" : ""}`}>{value}</div>
      <div className="text-[10px] text-muted-foreground">{label}</div>
    </div>
  )
}
