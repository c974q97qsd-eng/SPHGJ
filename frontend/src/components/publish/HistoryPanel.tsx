import { TaskTable } from "@/components/publish/TaskTable"

/** 发布记录:成功 / 失败 / 已跳过。失败的可在这里核对原因后重试。 */
export function HistoryPanel() {
  return <TaskTable mode="history" />
}
