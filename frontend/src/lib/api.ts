/** REST API 客户端:薄封装 fetch,统一错误处理。 */

export interface AccountStatus {
  id: string
  name: string
  logged_in: boolean
  running: boolean
  last_scan: string | null
  new_comments: number
  total_comments: number
  replied: number
  last_fetched: string | null
  auto_comment_enabled: boolean
  auto_comment_content: string
  has_aid: boolean
  wx_name: string
}

export interface EngineStatus {
  running: boolean
  accounts: AccountStatus[]
}

export interface Comment {
  account_id: string
  export_id: string
  comment_id: string
  nickname: string
  content: string
  head_url: string | null
  create_time: number
  like_count: number
  replied: boolean
  fetched_at: string | null
}

export interface CommentPage {
  items: Comment[]
  total: number
  limit: number
  offset: number
}

export interface AutoReplyConfig {
  enabled: boolean
  rules: { keyword: string; reply: string }[]
}

export interface AutoDeleteConfig {
  enabled: boolean
  keywords: string[]
}

export interface DeleteLogItem {
  account_id: string
  account_name: string
  comment_id: string
  export_id: string
  nickname: string
  content: string
  keyword: string
  deleted_at: string
}

export interface DeleteLogPage {
  items: DeleteLogItem[]
  total: number
  limit: number
  offset: number
}

export interface LiveStats {
  currentOnlineCount?: number
  totalAudienceCount?: number
  totalCheerCount?: number
  liveDurationInSeconds?: number
  newFollowCount?: number
  totalCommentCount?: number
  payedGmv?: number
  payedNum?: string
  privateDomainUv?: number
}

export interface LiveScreenItem {
  account_id: string
  name: string
  logged_in: boolean
  live_stats: LiveStats | null
  stream_url: string | null
  updated_at: string | null
  is_live?: boolean
  metrics?: Record<string, number | null>  // dashboardV4 指标(按 metrics 字典 key,见 MetricDef)
}

export type MetricFormat = "int" | "float" | "currency" | "yuan" | "percent" | "duration"

export interface MetricDef {
  key: string
  display_name: string
  unit: string
  format: MetricFormat
  group: string
}

export interface AppConfig {
  fetch_interval_sec: number
  auto_reply: AutoReplyConfig
  accounts_count: number
  card_fields: string[]
  dashboard_interval_sec: number
  live_check_interval_sec: number
}

/** 默认请求超时(毫秒)。后端若 10s 不响应则 abort,避免 fetch 永久 pending 导致页面卡死。 */
const FETCH_TIMEOUT_MS = 10_000

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS)
  try {
    const r = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
      signal: controller.signal,
    })
    if (!r.ok) {
      let msg = `${r.status}`
      try { msg += " " + (JSON.stringify(await r.json())) } catch { /* ignore */ }
      throw new Error(msg)
    }
    if (r.status === 204) return undefined as T
    return (await r.json()) as T
  } finally {
    clearTimeout(timer)
  }
}

export const api = {
  getConfig: () => req<AppConfig>("/config"),
  patchConfig: (body: Partial<{ fetch_interval_sec: number; auto_reply_enabled: boolean; card_fields: string[]; dashboard_interval_sec: number; live_check_interval_sec: number }>) =>
    req<{ ok: boolean; config: AppConfig }>("/config", { method: "PATCH", body: JSON.stringify(body) }),

  getAccounts: () => req<EngineStatus>("/accounts"),
  patchAccount: (id: string, body: Record<string, unknown>) =>
    req<{ ok: boolean; accounts: AccountStatus[] }>(`/accounts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  setAutoComment: (id: string, enabled: boolean, content: string) =>
    req<{ ok: boolean }>(`/accounts/${id}/auto-comment`, { method: "POST", body: JSON.stringify({ enabled, content }) }),
  deleteAccount: (id: string, remove_profile = false) =>
    req<{ ok: boolean; accounts: AccountStatus[] }>(`/accounts/${id}?remove_profile=${remove_profile}`, { method: "DELETE" }),
  startAccount: (id: string) => req<{ ok: boolean; logged_in: boolean }>(`/accounts/${id}/start`, { method: "POST" }),
  reloginAccount: (id: string) => req<{ sid: string; status: string }>(`/accounts/${id}/relogin`, { method: "POST" }),
  stopAccount: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/stop`, { method: "POST" }),
  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
  openDashboard: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-dashboard`, { method: "POST" }),

  loginStart: () => req<{ sid: string; status: string }>("/accounts/login/start", { method: "POST" }),
  loginOpenWindow: (sid: string) => req<{ sid: string; status: string }>(`/accounts/login/${sid}/open-window`, { method: "POST" }),
  loginCancel: (sid: string) => req<{ ok: boolean }>(`/accounts/login/${sid}/cancel`, { method: "POST" }),
  loginFinalize: (sid: string, account_id?: string, name?: string) =>
    req<{ ok: boolean; account: Record<string, unknown>; accounts: AccountStatus[] }>(`/accounts/login/${sid}/finalize`, {
      method: "POST",
      body: JSON.stringify({ account_id, name }),
    }),

  engineStart: () => req<EngineStatus>("/engine/start", { method: "POST" }),
  engineStop: () => req<EngineStatus>("/engine/stop", { method: "POST" }),
  engineFetchNow: () => req<{ ok: boolean }>("/engine/fetch-now", { method: "POST" }),

  getComments: (params: { account_id?: string; replied?: boolean; q?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<CommentPage>(`/comments?${q.toString()}`)
  },
  replyComment: (comment_id: string, account_id: string, content: string) =>
    req<{ ok: boolean }>(`/comments/${comment_id}/reply`, { method: "POST", body: JSON.stringify({ account_id, content }) }),
  deleteComment: (comment_id: string, account_id: string, export_id: string) =>
    req<{ ok: boolean }>(`/comments/${comment_id}`, { method: "DELETE", body: JSON.stringify({ account_id, export_id }) }),
  batchDeleteComments: (items: { comment_id: string; account_id: string; export_id: string }[]) =>
    req<{ ok: boolean; deleted: number; failed: { comment_id: string; error: string }[] }>("/comments/batch-delete", {
      method: "POST",
      body: JSON.stringify({ items }),
    }),
  pinComment: (comment_id: string, account_id: string, export_id: string, op_type = 1) =>
    req<{ ok: boolean }>(`/comments/${comment_id}/pin`, { method: "POST", body: JSON.stringify({ account_id, export_id, op_type }) }),

  getAutoReply: () => req<AutoReplyConfig>("/auto-reply/rules"),
  setAutoReply: (body: AutoReplyConfig) =>
    req<{ ok: boolean; auto_reply: AutoReplyConfig }>("/auto-reply/rules", { method: "PATCH", body: JSON.stringify(body) }),
  getAutoDelete: () => req<AutoDeleteConfig>("/auto-delete/rules"),
  setAutoDelete: (body: AutoDeleteConfig) =>
    req<{ ok: boolean; auto_delete: AutoDeleteConfig }>("/auto-delete/rules", { method: "PATCH", body: JSON.stringify(body) }),
  getDeleteLogs: (params: { account_id?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<DeleteLogPage>(`/auto-delete/logs?${q.toString()}`)
  },
  clearDeleteLogs: (account_id?: string) =>
    req<{ ok: boolean }>(`/auto-delete/logs${account_id ? `?account_id=${account_id}` : ""}`, { method: "DELETE" }),

  getLiveScreenStatus: () => req<{ items: LiveScreenItem[]; card_fields: string[] }>("/live-screen/status"),
  getMetricsDictionary: () => req<{ metrics: MetricDef[]; card_fields: string[] }>("/metrics/dictionary"),

  exportCsvUrl: (account_id?: string) => `/api/comments/export${account_id ? `?account_id=${account_id}` : ""}`,
}
