import type { ReactNode } from "react"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"

export interface PickerOption {
  /** "" 不安全:Radix Tabs 要求非空 value,调用方用 "__all__"/"__generic__" 这类哨兵 */
  value: string
  label: string
  /** 卡片右侧的小数字(待发 N / 标题剩 K),不传就不显示 */
  count?: number
  /** hover 提示:放「共 M 条」「今日已发 N」这类次要信息,不占版面 */
  hint?: string
  icon?: ReactNode
}

/**
 * 账号选项卡片。
 *
 * 样式与「发布管理」顶部页签完全一致(TabsList 灰底胶囊 + 选中项白底描边),
 * 用的就是 @/components/ui/tabs —— 不另造一套视觉,免得同页两种风格。
 *
 * 为什么不用下拉框:账号一多,下拉里只剩一串名字,既看不出各账号还剩多少活,
 * 又要两次点击才能切换。卡片全摊开、带数字,一眼就能挑。
 */
export function AccountPicker({
  value,
  onChange,
  options,
  className,
  ariaLabel,
}: {
  value: string
  onChange: (v: string) => void
  options: PickerOption[]
  className?: string
  ariaLabel?: string
}) {
  if (!options.length) return null
  return (
    <Tabs value={value} onValueChange={onChange} className="min-w-0 max-w-full">
      <TabsList
        aria-label={ariaLabel}
        // 账号多时横向滚动,不换行把页头撑高
        className={cn("h-9 max-w-full justify-start overflow-x-auto", className)}
      >
        {options.map((o) => (
          <TabsTrigger key={o.value} value={o.value} title={o.hint} className="shrink-0 gap-1.5">
            {o.icon}
            <span className="max-w-[8rem] truncate">{o.label}</span>
            {o.count !== undefined && (
              <span className="rounded bg-muted px-1 text-[10px] font-normal tabular-nums text-muted-foreground">
                {o.count}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  )
}
