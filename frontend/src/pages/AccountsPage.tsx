import { useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { StatusBadge } from "@/components/common/status-badge"
import { Badge } from "@/components/ui/badge"
import { EmptyState, LoadingState, ErrorState } from "@/components/common/states"
import { AddAccountDialog } from "@/components/accounts/AddAccountDialog"
import { useAccounts, useAccountActions, type AccountStatus } from "@/hooks/useAccounts"
import { api } from "@/lib/api"
import { fmtTime } from "@/lib/utils"
import { toast } from "sonner"
import { Plus, Play, Square, Trash2, Save, Loader2, MessageSquarePlus, Users, ExternalLink, ChevronRight, Lock, Unlock } from "lucide-react"

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
  const { busy, start, stop, openBrowser, remove, setLock } = useAccountActions(onRefresh)
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

  // 锁定/解锁微信身份:锁定后该卡片只接受锁定的那个微信登录
  const handleToggleLock = async () => {
    try {
      if (acc.locked) {
        await setLock(acc.id, false)
        toast.success("已解锁,可接受任意微信登录")
      } else {
        await setLock(acc.id, true)
        toast.success(`已锁定微信「${acc.name}」,其他微信登录不会被保存`)
      }
    } catch (e) {
      toast.error("操作失败:" + (e as Error).message)
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
              {acc.locked && (
                <Badge variant="outline" className="gap-1 border-amber-500/40 text-amber-600" title={`已锁定微信:${acc.locked_name}`}>
                  <Lock className="h-3 w-3" />已锁定
                </Badge>
              )}
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
            <Button
              size="icon"
              variant="ghost"
              onClick={handleToggleLock}
              disabled={isBusy}
              title={acc.locked ? `已锁定「${acc.locked_name}」,点击解锁` : "锁定当前微信(之后只接受该微信登录)"}
              aria-label={acc.locked ? "解锁微信绑定" : "锁定当前微信"}
            >
              {acc.locked ? <Lock className="h-4 w-4 text-amber-600" /> : <Unlock className="h-4 w-4" />}
            </Button>
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
  const [open, setOpen] = useState(false)

  const save = async () => {
    if (enabled && !content.trim()) { toast.error("启用自动评论需填写内容"); return }
    setSaving(true)
    try { await api.setAutoComment(acc.id, enabled, content); toast.success("自动评论已保存") }
    catch (e) { toast.error((e as Error).message) }
    finally { setSaving(false) }
  }

  return (
    <div className="space-y-2 rounded-md border p-3">
      {/* 折叠行:默认收起,点击展开配置(自动隐藏模式)。用 div+role 避免 button 内嵌 Switch(button) 的非法嵌套 */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((v) => !v) } }}
        className="flex w-full cursor-pointer items-center justify-between gap-2 rounded text-left focus:outline-none focus-visible:ring-1 focus-visible:ring-primary"
        aria-expanded={open}
      >
        <div className="flex min-w-0 items-center gap-2">
          <ChevronRight className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`} />
          <MessageSquarePlus className="h-4 w-4 shrink-0 text-primary" />
          <Label className="cursor-pointer truncate text-sm font-medium">自动评论</Label>
          {enabled && <Badge variant="secondary" className="shrink-0 font-normal">已启用</Badge>}
        </div>
        <div onClick={(e) => e.stopPropagation()}>
          <Switch checked={enabled} onCheckedChange={setEnabled} aria-label="启用自动评论" />
        </div>
      </div>
      {open && (
        <div className="space-y-2 pt-1">
          <p className="text-xs text-muted-foreground">检测到新视频发布后自动发送评论并置顶。关闭开关可停用,内容会保留。</p>
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
      )}
    </div>
  )
}

function Stat({ label, value, accent, small }: { label: string; value: number | string; accent?: boolean; small?: boolean }) {
  return (
    <div className="rounded-md bg-muted/40 px-2 py-1.5">
      <div className={`font-semibold ${small ? "text-xs" : "text-lg"} ${accent ? "text-primary" : ""}`}>{value}</div>
      <div className="text-[0.625rem] text-muted-foreground">{label}</div>
    </div>
  )
}
