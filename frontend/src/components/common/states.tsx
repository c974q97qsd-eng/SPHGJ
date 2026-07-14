import { type ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Loader2 } from "lucide-react"

/** 空数据态:图标 + 标题 + 描述 + 可选操作。 */
export function EmptyState({
  icon, title, description, action, className,
}: { icon?: ReactNode; title: string; description?: string; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 py-16 text-center", className)}>
      {icon && <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">{icon}</div>}
      <div className="space-y-1">
        <p className="text-sm font-medium">{title}</p>
        {description && <p className="text-sm text-muted-foreground max-w-sm">{description}</p>}
      </div>
      {action}
    </div>
  )
}

/** 加载态。 */
export function LoadingState({ text = "加载中…", className }: { text?: string; className?: string }) {
  return (
    <div className={cn("flex items-center justify-center gap-2 py-12 text-muted-foreground", className)}>
      <Loader2 className="h-4 w-4 animate-spin" />
      <span className="text-sm">{text}</span>
    </div>
  )
}

/** 错误态 + 重试。 */
export function ErrorState({ message, onRetry, className }: { message: string; onRetry?: () => void; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 py-12 text-center", className)}>
      <p className="text-sm text-destructive">{message}</p>
      {onRetry && (
        <button onClick={onRetry} className="text-sm text-primary hover:underline">重试</button>
      )}
    </div>
  )
}
