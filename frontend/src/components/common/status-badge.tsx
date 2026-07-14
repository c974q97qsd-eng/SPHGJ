import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

/** 账号/引擎状态徽标。 */
export function StatusBadge({ state, className }: { state: "online" | "offline" | "running" | "idle" | "warning"; className?: string }) {
  const map = {
    online: { label: "在线", variant: "success" as const, dot: "bg-success" },
    offline: { label: "离线", variant: "secondary" as const, dot: "bg-muted-foreground" },
    running: { label: "运行中", variant: "success" as const, dot: "bg-success animate-pulse" },
    idle: { label: "未启动", variant: "outline" as const, dot: "bg-muted-foreground" },
    warning: { label: "未登录", variant: "warning" as const, dot: "bg-warning" },
  }
  const c = map[state]
  return (
    <Badge variant={c.variant} className={cn("gap-1.5 font-normal", className)}>
      <span className={cn("h-1.5 w-1.5 rounded-full", c.dot)} />
      {c.label}
    </Badge>
  )
}
