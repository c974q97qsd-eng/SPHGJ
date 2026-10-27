import { Button } from "@/components/ui/button"
import { ChevronLeft, ChevronRight } from "lucide-react"

/**
 * 通用分页条（铁律：所有内容列表都必须分页）。
 *
 * 为什么强制：列表一次性渲染几百行 DOM，滚动和重排都会明显卡；而且后端
 * 接口本来就有 offset/total，前端不翻页等于白拿。
 *
 * 只有一页时不渲染（不占版面），调用方不用自己判断。
 */
export function Pager({
  offset,
  limit,
  total,
  onChange,
  unit = "条",
}: {
  offset: number
  limit: number
  total: number
  onChange: (nextOffset: number) => void
  unit?: string
}) {
  if (total <= limit) return null

  const pageCount = Math.max(1, Math.ceil(total / limit))
  const page = Math.floor(offset / limit) + 1
  const from = Math.min(offset + 1, total)
  const to = Math.min(offset + limit, total)

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
      <span className="text-xs text-muted-foreground">
        第 {from}-{to} / 共 {total} {unit} · 第 {page}/{pageCount} 页
      </span>
      <div className="flex items-center gap-1.5">
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs"
          disabled={offset <= 0}
          onClick={() => onChange(0)}>
          首页
        </Button>
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs"
          disabled={offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}>
          <ChevronLeft className="h-3.5 w-3.5" />上一页
        </Button>
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}>
          下一页<ChevronRight className="h-3.5 w-3.5" />
        </Button>
        <Button variant="outline" size="sm" className="h-7 gap-1 px-2 text-xs"
          disabled={offset + limit >= total}
          onClick={() => onChange((pageCount - 1) * limit)}>
          末页
        </Button>
      </div>
    </div>
  )
}
