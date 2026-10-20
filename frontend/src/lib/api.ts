/** REST API 客户端:薄封装 fetch,统一错误处理。 */
import type { LogLine } from "@/lib/ws"

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
  login_at: string | null
  /** 手动退出登录态的时间(rev22);未退出过为 undefined */
  logout_at?: string | null
  locked: boolean
  locked_name: string
}

export interface EngineStatus {
  running: boolean
  accounts: AccountStatus[]
}

export interface Comment {
  /** 归属视频号(评论是视频号的资产,不是登录卡片的) */
  finder_id: string
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

/** 评论归属视频号:评论数、最后抓取时间、承载卡片与是否已登录 */
export interface CommentFinder {
  finder_id: string
  name: string
  account_id: string
  account_ids: string[]
  logged_in: boolean
  comments: number
  last_fetched: string | null
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
  finder_id: string
  finder_name: string
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
  /** 界面缩放档位:large=大(100%) / medium=中(88%) / small=小(75%) */
  ui_scale?: "large" | "medium" | "small"
  posts?: {
    auto_refresh_enabled: boolean
    auto_refresh_hour: number
    refresh_cooldown_sec: number
    max_pages: number
  }
}

export interface PostItem {
  /** 归属主键:视频号 ID(作品属于视频号,不属于登录卡片) */
  finder_id: string
  /** 视频号展示名(当前承载它的账号卡片名;卡片删了显示「视频号 xxxx」) */
  finder_name?: string
  /** 最后一次抓取该作品的登录卡片(仅追溯,不参与归属) */
  account_id: string
  account_name?: string
  object_id: string
  export_id: string
  title: string
  cover_url: string
  cover_path: string
  create_time: number
  read_count: number
  like_count: number
  comment_count: number
  forward_count: number
  fav_count: number
  visible_type: number
  sticky_op: number
  is_hidden: boolean
  is_sticky: boolean
  updated_at: string | null
}

export interface PostPage {
  items: PostItem[]
  total: number
  limit: number
  offset: number
}

export interface PostMeta {
  last_refresh: string | null
  last_pages: number
  today_requests: number
  full_synced: number
  account_id?: string
}

/** 作品归属视频号(作品管理的主维度) */
export interface PostFinder {
  finder_id: string
  name: string
  account_id: string
  account_ids: string[]
  logged_in: boolean
  /** 当前是否有账号卡片承载该视频号(卡片删掉后为 false,作品仍然存在) */
  bound: boolean
  works: number
  hidden: number
  sticky: number
  last_create: number | null
  last_update: string | null
  meta: PostMeta
}

export interface PostsJobState {
  running: boolean
  kind: "refresh" | "batch" | null
  accounts: Record<string, Record<string, unknown>>
  total: number
  done: number
  failed: { object_id?: string; error: string }[]
  started_at: string | null
  finished_at: string | null
}

/** 运行日志拉取结果(seq 为当前最大序号,用于增量续拉)。 */
export interface LogsResult {
  lines: LogLine[]
  seq: number
}

// ==================== 自动发作品(rev25) ====================
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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  if (!r.ok) {
    let msg = `${r.status}`
    try {
      const j = await r.json()
      // 优先展示后端 detail(如锁定拒绝原因),便于用户直接看懂
      msg = j?.detail ?? JSON.stringify(j)
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  if (r.status === 204) return undefined as T
  return (await r.json()) as T
}

export type PostPlayResult = {
  ok: boolean
  url?: string
  title?: string
  message?: string
  refreshed?: boolean
}

export type RemoteSession = {
  account_id: string
  url: string
  idle_sec: number
  created_at: number
}

export const api = {
  // 运行日志(after=0 拉全量;轮询时用上次 seq 拉增量)
  getLogs: (after = 0) => req<LogsResult>(`/logs?after=${after}`),
  clearLogs: () => req<{ ok: boolean }>("/logs", { method: "DELETE" }),

  getConfig: () => req<AppConfig>("/config"),
  patchConfig: (body: Partial<{
    fetch_interval_sec: number
    auto_reply_enabled: boolean
    card_fields: string[]
    dashboard_interval_sec: number
    live_check_interval_sec: number
    ui_scale: "large" | "medium" | "small"
    posts_auto_refresh_enabled: boolean
    posts_auto_refresh_hour: number
  }>) =>
    req<{ ok: boolean; config: AppConfig }>("/config", { method: "PATCH", body: JSON.stringify(body) }),

  getAccounts: () => req<EngineStatus>("/accounts"),
  // rev31: 本机可见浏览器窗口(独立 profile,不影响后台抓取/直播)
  openAccountWindow: (id: string) =>
    req<{ ok: boolean; account_id: string; name: string; temp_profile: boolean; injected: boolean; already?: boolean; hint?: string }>(
      `/accounts/${id}/open-window`, { method: "POST" }),
  closeAccountWindow: (id: string) =>
    req<{ ok: boolean; account_id: string; reason?: string }>(
      `/accounts/${id}/close-window`, { method: "POST" }),
  // rev33: 当前访问者是否「运行程序的这台机器」(决定是否显示「打开」按钮)
  whoami: () => req<{ local: boolean; client: string }>("/system/whoami"),
  getLocalWindows: () =>
    req<{ items: { account_id: string; name: string; opened_sec: number; idle_sec: number; temp_profile: boolean; injected: boolean }[] }>(
      "/local-windows"),
  patchAccount: (id: string, body: Record<string, unknown>) =>
    req<{ ok: boolean; accounts: AccountStatus[] }>(`/accounts/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  setAutoComment: (id: string, enabled: boolean, content: string) =>
    req<{ ok: boolean }>(`/accounts/${id}/auto-comment`, { method: "POST", body: JSON.stringify({ enabled, content }) }),
  deleteAccount: (id: string, remove_profile = false) =>
    req<{ ok: boolean; accounts: AccountStatus[] }>(`/accounts/${id}?remove_profile=${remove_profile}`, { method: "DELETE" }),
  startAccount: (id: string) => req<{ ok: boolean; logged_in: boolean }>(`/accounts/${id}/start`, { method: "POST" }),
  reloginAccount: (id: string, headed?: boolean) =>
    req<{ sid: string; status: string; headed?: boolean }>(`/accounts/${id}/relogin${headed === undefined ? "" : `?headed=${headed}`}`, { method: "POST" }),
  stopAccount: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/stop`, { method: "POST" }),
  lockAccount: (id: string) =>
    req<{ ok: boolean; locked: boolean; accounts: AccountStatus[] }>(`/accounts/${id}/lock`, { method: "POST" }),
  unlockAccount: (id: string) =>
    req<{ ok: boolean; locked: boolean; accounts: AccountStatus[] }>(`/accounts/${id}/unlock`, { method: "POST" }),
  /** 退出登录态:清该账号 profile 内登录文件,账号保留(下次需重新扫码) */
  logoutAccount: (id: string) =>
    req<{ ok: boolean; message: string; accounts: AccountStatus[] }>(`/accounts/${id}/logout`, { method: "POST" }),
  openAccountBrowser: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-browser`, { method: "POST" }),
  openDashboard: (id: string) => req<{ ok: boolean }>(`/accounts/${id}/open-dashboard`, { method: "POST" }),

  loginStart: (headed?: boolean) =>
    req<{ sid: string; status: string; headed?: boolean }>(`/accounts/login/start${headed === undefined ? "" : `?headed=${headed}`}`, { method: "POST" }),
  selectLoginAccount: (sid: string, index: number) =>
    req<{ ok: boolean }>(`/accounts/login/${sid}/select-account`, { method: "POST", body: JSON.stringify({ index }) }),
  loginOpenWindow: (sid: string) => req<{ sid: string; status: string }>(`/accounts/login/${sid}/open-window`, { method: "POST" }),
  loginCancel: (sid: string) => req<{ ok: boolean }>(`/accounts/login/${sid}/cancel`, { method: "POST" }),
  /** 扫错微信(非锁定微信)确认后:重新进入全新登录环境等待扫码(登录态已由后端清除) */
  loginRetryClean: (sid: string) =>
    req<{ sid: string; status: string; headed?: boolean }>(`/accounts/login/${sid}/retry-clean`, { method: "POST" }),
  loginFinalize: (sid: string, account_id?: string, name?: string) =>
    req<{ ok: boolean; account: Record<string, unknown>; accounts: AccountStatus[] }>(`/accounts/login/${sid}/finalize`, {
      method: "POST",
      body: JSON.stringify({ account_id, name }),
    }),

  engineStart: () => req<EngineStatus>("/engine/start", { method: "POST" }),
  engineStop: () => req<EngineStatus>("/engine/stop", { method: "POST" }),
  engineFetchNow: () => req<{ ok: boolean }>("/engine/fetch-now", { method: "POST" }),

  getComments: (params: { finder_id?: string; account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<CommentPage>(`/comments?${q.toString()}`)
  },
  getCommentFinders: () => req<{ items: CommentFinder[] }>("/comments/finders"),
  // 写操作一律带 finder_id:卡片删了/换了也能找到登录着该视频号的账号来执行
  replyComment: (comment_id: string, finder_id: string, account_id: string, content: string) =>
    req<{ ok: boolean }>(`/comments/${comment_id}/reply`, { method: "POST", body: JSON.stringify({ finder_id, account_id, content }) }),
  deleteComment: (comment_id: string, finder_id: string, account_id: string, export_id: string) =>
    req<{ ok: boolean }>(`/comments/${comment_id}`, { method: "DELETE", body: JSON.stringify({ finder_id, account_id, export_id }) }),
  batchDeleteComments: (items: { finder_id?: string; comment_id: string; account_id: string; export_id: string }[]) =>
    req<{ ok: boolean; deleted: number; failed: { comment_id: string; error: string }[] }>("/comments/batch-delete", {
      method: "POST",
      body: JSON.stringify({ items }),
    }),
  pinComment: (comment_id: string, finder_id: string, account_id: string, export_id: string, op_type = 1) =>
    req<{ ok: boolean }>(`/comments/${comment_id}/pin`, { method: "POST", body: JSON.stringify({ finder_id, account_id, export_id, op_type }) }),

  getAutoReply: () => req<AutoReplyConfig>("/auto-reply/rules"),
  setAutoReply: (body: AutoReplyConfig) =>
    req<{ ok: boolean; auto_reply: AutoReplyConfig }>("/auto-reply/rules", { method: "PATCH", body: JSON.stringify(body) }),
  getAutoDelete: () => req<AutoDeleteConfig>("/auto-delete/rules"),
  setAutoDelete: (body: AutoDeleteConfig) =>
    req<{ ok: boolean; auto_delete: AutoDeleteConfig }>("/auto-delete/rules", { method: "PATCH", body: JSON.stringify(body) }),
  getDeleteLogs: (params: { finder_id?: string; account_id?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<DeleteLogPage>(`/auto-delete/logs?${q.toString()}`)
  },
  clearDeleteLogs: (finder_id?: string) =>
    req<{ ok: boolean }>(`/auto-delete/logs${finder_id ? `?finder_id=${encodeURIComponent(finder_id)}` : ""}`, { method: "DELETE" }),

  // ==================== 作品管理 ====================
  getPosts: (params: { finder_id?: string; account_id?: string; q?: string; visible?: string; date_from?: string; date_to?: string; sort?: string; order?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<PostPage>(`/posts?${q.toString()}`)
  },
  getPostFinders: () => req<{ items: PostFinder[] }>("/posts/finders"),
  getPostsMeta: () => req<{ meta: Record<string, PostMeta> }>("/posts/meta"),
  postsRefresh: (account_id?: string) =>
    req<{ ok: boolean; accounts: string[] }>("/posts/refresh", { method: "POST", body: JSON.stringify({ account_id }) }),
  postsBatch: (action: "hide" | "unhide" | "sticky" | "unsticky", items: { finder_id: string; object_id: string }[]) =>
    req<{ ok: boolean; total: number }>("/posts/batch", { method: "POST", body: JSON.stringify({ action, items }) }),
  postsJob: () => req<PostsJobState>("/posts/job"),

  getLiveScreenStatus: () => req<{ items: LiveScreenItem[]; card_fields: string[] }>("/live-screen/status"),
  getMetricsDictionary: () => req<{ metrics: MetricDef[]; card_fields: string[] }>("/metrics/dictionary"),

  // ==================== 自动发作品 ====================
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
  // 作品播放:后端会校验 CDN 地址时效,过期就用当前登录态现取
  postPlay: (finder_id: string, object_id: string) =>
    req<PostPlayResult>(`/posts/play?finder_id=${encodeURIComponent(finder_id)}&object_id=${encodeURIComponent(object_id)}`),

  // 远程同步浏览
  remoteSessions: () => req<{ items: RemoteSession[] }>("/remote/sessions"),
  remoteClose: (account: string) =>
    req<{ ok: boolean }>(`/remote/close?account=${encodeURIComponent(account)}`, { method: "POST" }),

  publishClearTitles: (finder_id?: string, only_used = false) =>
    req<{ ok: boolean; deleted: number }>("/publish/titles/clear", { method: "POST", body: JSON.stringify({ finder_id, only_used }) }),
  publishSaveSettings: (body: Record<string, unknown>) =>
    req<{ ok: boolean; cfg: Record<string, unknown> }>("/publish/settings", { method: "POST", body: JSON.stringify(body) }),
  publishSaveMappings: (mappings: Record<string, string>) =>
    req<{ ok: boolean; mappings: Record<string, string> }>("/publish/mappings", { method: "POST", body: JSON.stringify({ mappings }) }),

  exportCsvUrl: (finder_id?: string) => `/api/comments/export${finder_id ? `?finder_id=${encodeURIComponent(finder_id)}` : ""}`,
}
