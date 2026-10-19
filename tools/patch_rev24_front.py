# -*- coding: utf-8 -*-
"""rev24 补丁 4/4:前端 api 层 / hooks / 删除记录面板 改按视频号归属。"""
import io
import sys

API = r"D:\SPHGJ\frontend\src\lib\api.ts"
HOOK = r"D:\SPHGJ\frontend\src\hooks\useComments.ts"
PANEL = r"D:\SPHGJ\frontend\src\components\comments\AutoDeletePanel.tsx"

PATCHES = {API: [], HOOK: [], PANEL: []}


def p(path, tag, old, new):
    PATCHES[path].append((tag, old, new))


# ============================================================ api.ts
p(API, "comment-type", '''export interface Comment {
  account_id: string''',
  '''export interface Comment {
  /** 归属视频号(评论是视频号的资产,不是登录卡片的) */
  finder_id: string
  account_id: string''')

p(API, "finder-type", '''export interface CommentPage {
  items: Comment[]
  total: number
  limit: number
  offset: number
}''',
  '''export interface CommentPage {
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
}''')

p(API, "deletelog-type", '''export interface DeleteLogItem {
  account_id: string
  account_name: string''',
  '''export interface DeleteLogItem {
  finder_id: string
  finder_name: string
  account_id: string
  account_name: string''')

p(API, "get-comments", '''  getComments: (params: { account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number } = {}) => {
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
    req<{ ok: boolean }>(`/comments/${comment_id}/pin`, { method: "POST", body: JSON.stringify({ account_id, export_id, op_type }) }),''',
  '''  getComments: (params: { finder_id?: string; account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number } = {}) => {
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
    req<{ ok: boolean }>(`/comments/${comment_id}/pin`, { method: "POST", body: JSON.stringify({ finder_id, account_id, export_id, op_type }) }),''')

p(API, "del-logs", '''  getDeleteLogs: (params: { account_id?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<DeleteLogPage>(`/auto-delete/logs?${q.toString()}`)
  },
  clearDeleteLogs: (account_id?: string) =>
    req<{ ok: boolean }>(`/auto-delete/logs${account_id ? `?account_id=${account_id}` : ""}`, { method: "DELETE" }),''',
  '''  getDeleteLogs: (params: { finder_id?: string; account_id?: string; q?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<DeleteLogPage>(`/auto-delete/logs?${q.toString()}`)
  },
  clearDeleteLogs: (finder_id?: string) =>
    req<{ ok: boolean }>(`/auto-delete/logs${finder_id ? `?finder_id=${encodeURIComponent(finder_id)}` : ""}`, { method: "DELETE" }),''')

p(API, "export", '''  exportCsvUrl: (account_id?: string) => `/api/comments/export${account_id ? `?account_id=${account_id}` : ""}`,''',
  '''  exportCsvUrl: (finder_id?: string) => `/api/comments/export${finder_id ? `?finder_id=${encodeURIComponent(finder_id)}` : ""}`,''')

# ============================================================ useComments.ts
p(HOOK, "params", '''export function useComments(params: { account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number }) {''',
  '''export function useComments(params: { finder_id?: string; account_id?: string; replied?: boolean; q?: string; hide_own?: number; limit?: number; offset?: number }) {''')

# ============================================================ AutoDeletePanel.tsx
p(PANEL, "badge", '''                      <Badge variant="secondary" className="shrink-0 font-normal">{log.account_name}</Badge>''',
  '''                      <Badge variant="secondary" className="shrink-0 font-normal">{log.finder_name || log.account_name}</Badge>''')

# ============================================================ apply
fail = []
for path, items in PATCHES.items():
    s = io.open(path, encoding="utf-8").read()
    for tag, old, new in items:
        n = s.count(old)
        if n != 1:
            fail.append(f"{path.split(chr(92))[-1]}:{tag} 命中 {n} 次")
            continue
        s = s.replace(old, new, 1)
    if not fail:
        io.open(path, "w", encoding="utf-8", newline="").write(s)
if fail:
    print("PATCH FAIL")
    for f in fail:
        print("  -", f)
    sys.exit(1)

# ============================================================ selfcheck
a = io.open(API, encoding="utf-8").read()
h = io.open(HOOK, encoding="utf-8").read()
pa = io.open(PANEL, encoding="utf-8").read()
checks = [
    ("finder_id: string\n  account_id: string" in a, "Comment.finder_id"),
    ("export interface CommentFinder" in a, "CommentFinder"),
    ("getCommentFinders" in a, "finders api"),
    ("replyComment: (comment_id: string, finder_id: string" in a, "reply 带 finder"),
    ("finder_id: string\n  finder_name: string" in a, "DeleteLogItem"),
    ("exportCsvUrl: (finder_id?: string)" in a, "导出按视频号"),
    ("finder_id?: string; account_id?: string; replied?: boolean" in h, "hook params"),
    ("log.finder_name || log.account_name" in pa, "删除记录显示视频号"),
]
bad = [t for ok, t in checks if not ok]
print("SELFCHECK", "PASS" if not bad else "FAIL: " + ", ".join(bad))
sys.exit(0 if not bad else 2)
