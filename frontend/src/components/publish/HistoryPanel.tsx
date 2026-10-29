import { useState } from "react"
import { AccountPicker } from "@/components/publish/AccountPicker"
import { TaskTable } from "@/components/publish/TaskTable"
import { historyCountOf, usePublishFinders } from "@/hooks/usePublishFinders"
import { CircleUser, Users } from "lucide-react"

/** 发布记录:成功 / 失败 / 已跳过。失败的可在这里核对原因后重试。 */
export function HistoryPanel() {
  const { finders } = usePublishFinders()
  // "" 不能当 Tabs value,用哨兵表示「全部」
  const [finder, setFinder] = useState("__all__")

  return (
    <div className="space-y-3">
      {/* 账号选项卡片:与「待发布」「标题池」同一套样式,选中哪个就只看哪个账号的记录 */}
      <div className="flex flex-wrap items-center gap-2">
        <AccountPicker
          value={finder}
          onChange={setFinder}
          ariaLabel="按账号筛选发布记录"
          options={[
            {
              value: "__all__", label: "全部", icon: <Users className="h-3.5 w-3.5" />,
              count: finders.reduce((a, f) => a + historyCountOf(f), 0),
              hint: "所有视频号的发布记录合计",
            },
            ...finders.map((f) => ({
              value: f.finder_id,
              label: f.name,
              icon: <CircleUser className="h-3.5 w-3.5" />,
              count: historyCountOf(f),
              hint: `${f.name} · 记录 ${historyCountOf(f)} 条` +
                (f.today_success ? ` · 今日已发 ${f.today_success} 条` : "") +
                (f.last_success_at ? ` · 最近成功 ${f.last_success_at.replace("T", " ").slice(5, 16)}` : ""),
            })),
          ]}
        />
      </div>

      <TaskTable mode="history" finderId={finder === "__all__" ? "" : finder} />
    </div>
  )
}
