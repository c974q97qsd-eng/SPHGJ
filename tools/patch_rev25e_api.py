# -*- coding: utf-8 -*-
"""rev25-4a: 前端 api.ts 增加「自动发作品」类型与接口。"""
import io, sys

P = r"D:\SPHGJ\frontend\src\lib\api.ts"
s = io.open(P, encoding="utf-8").read()
orig = len(s)

assert "PublishTask" not in s, "似乎已打过补丁"

# 1) 类型:插在 req 函数之前
TYPES = '''// ==================== 自动发作品(rev25) ====================
export type PublishTaskStatus = "pending" | "running" | "success" | "failed" | "skipped" | "need_review"

export interface PublishTask {
  id: number
  finder_id: string
  finder_name?: string
  account_id: string
  src_path: string
  file_name: string
  file_size: number
  file_fp: string
  title: string
  title_id: number
  status: PublishTaskStatus
  /** 执行到哪一步:prepare/open/upload/edit/fill/submit/verify/done */
  step: string
  attempts: number
  last_error: string
  object_id: string
  export_id: string
  queued_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface PublishTitle {
  id: number
  finder_id: string
  content: string
  enabled: number
  used_count: number
  used_task_id: number
  last_used_at: string | null
  created_at: string | null
}

export interface PublishFinder {
  finder_id: string
  name: string
  account_id: string
  running: boolean
  logged_in: boolean
  human_stopped: boolean
  today_success: number
  titles_total: number
  titles_unused: number
  last_success_at: string
  fail_streak: number
}

export interface PublishStatus {
  enabled: boolean
  inbox_dir: string
  busy: boolean
  current_task: number | null
  pending: number
  running: number
  need_review: number
  today_success: Record<string, number>
  unbound: { dir: string; files: number; reason: string }[]
  fail_streak: Record<string, number>
  in_window: boolean
  last_scan: Record<string, unknown>
  cfg: Record<string, unknown>
}

'''
OLD = "async function req<T>(path: string, init?: RequestInit): Promise<T> {"
assert s.count(OLD) == 1
s = s.replace(OLD, TYPES + OLD, 1)

# 2) 方法:插在 exportCsvUrl 之前
METHODS = '''  // ==================== 自动发作品 ====================
  publishStatus: () => req<PublishStatus>("/publish/status"),
  publishFinders: () => req<{ items: PublishFinder[]; generic_titles: { total: number; unused: number }; mappings: Record<string, string> }>("/publish/finders"),
  publishTasks: (params: { status?: string; finder_id?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<{ items: PublishTask[]; total: number }>(`/publish/tasks?${q.toString()}`)
  },
  publishScan: () => req<{ ok: boolean; scan: Record<string, unknown>; titles_filled: number }>("/publish/scan", { method: "POST" }),
  publishRun: (id: number) => req<{ ok: boolean; result: { ok?: boolean; error?: string } }>(`/publish/tasks/${id}/run`, { method: "POST" }),
  publishSkip: (id: number) => req<{ ok: boolean }>(`/publish/tasks/${id}/skip`, { method: "POST" }),
  publishRetry: (id: number) => req<{ ok: boolean }>(`/publish/tasks/${id}/retry`, { method: "POST" }),
  publishSetTitle: (id: number, title: string) =>
    req<{ ok: boolean }>(`/publish/tasks/${id}/title`, { method: "POST", body: JSON.stringify({ title }) }),
  publishDeleteTask: (id: number) => req<{ ok: boolean }>(`/publish/tasks/${id}`, { method: "DELETE" }),
  publishTitles: (finder_id?: string) =>
    req<{ items: PublishTitle[]; stats: Record<string, { total: number; unused: number }> }>(
      `/publish/titles${finder_id !== undefined ? `?finder_id=${encodeURIComponent(finder_id)}` : ""}`),
  publishAddTitles: (finder_id: string, contents: string) =>
    req<{ ok: boolean; added: number; titles_filled: number }>("/publish/titles", { method: "POST", body: JSON.stringify({ finder_id, contents }) }),
  publishDeleteTitle: (id: number) => req<{ ok: boolean }>(`/publish/titles/${id}`, { method: "DELETE" }),
  publishClearTitles: (finder_id?: string, only_used = false) =>
    req<{ ok: boolean; deleted: number }>("/publish/titles/clear", { method: "POST", body: JSON.stringify({ finder_id, only_used }) }),
  publishSaveSettings: (body: Record<string, unknown>) =>
    req<{ ok: boolean; cfg: Record<string, unknown> }>("/publish/settings", { method: "POST", body: JSON.stringify(body) }),
  publishSaveMappings: (mappings: Record<string, string>) =>
    req<{ ok: boolean; mappings: Record<string, string> }>("/publish/mappings", { method: "POST", body: JSON.stringify({ mappings }) }),

'''
OLD2 = "  exportCsvUrl: (finder_id?: string) =>"
assert s.count(OLD2) == 1
s = s.replace(OLD2, METHODS + OLD2, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
chk = io.open(P, encoding="utf-8").read()
must = ["export interface PublishTask", "export interface PublishStatus", "publishStatus:",
        "publishTasks:", "publishAddTitles:", "publishSaveMappings:"]
miss = [m for m in must if m not in chk]
print(f"size {orig} -> {len(chk)}")
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
