# -*- coding: utf-8 -*-
"""rev23-frontend-api: PostItem 增加视频号归属字段;新增 getPostFinders。"""
import io, sys

P = r"D:\SPHGJ\frontend\src\lib\api.ts"
s = io.open(P, encoding="utf-8").read()
orig = s

old = '''export interface PostItem {
  account_id: string
  account_name?: string
  object_id: string'''
new = '''export interface PostItem {
  /** 归属主键:视频号 ID(作品属于视频号,不属于登录卡片) */
  finder_id: string
  /** 视频号展示名(当前承载它的账号卡片名;卡片删了显示「视频号 xxxx」) */
  finder_name?: string
  /** 最后一次抓取该作品的登录卡片(仅追溯,不参与归属) */
  account_id: string
  account_name?: string
  object_id: string'''
assert s.count(old) == 1, "PostItem"
s = s.replace(old, new, 1)

old = '''export interface PostMeta {
  last_refresh: string | null
  last_pages: number
  today_requests: number
  full_synced: number
}'''
new = '''export interface PostMeta {
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
}'''
assert s.count(old) == 1, "PostMeta"
s = s.replace(old, new, 1)

old = '''  getPosts: (params: { account_id?: string; q?: string; visible?: string; date_from?: string; date_to?: string; sort?: string; order?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<PostPage>(`/posts?${q.toString()}`)
  },
  getPostsMeta: () => req<{ meta: Record<string, PostMeta> }>("/posts/meta"),
  postsRefresh: (account_id?: string) =>
    req<{ ok: boolean; accounts: string[] }>("/posts/refresh", { method: "POST", body: JSON.stringify({ account_id }) }),
  postsBatch: (action: "hide" | "unhide" | "sticky" | "unsticky", items: { account_id: string; object_id: string }[]) =>
    req<{ ok: boolean; total: number }>("/posts/batch", { method: "POST", body: JSON.stringify({ action, items }) }),'''
new = '''  getPosts: (params: { finder_id?: string; account_id?: string; q?: string; visible?: string; date_from?: string; date_to?: string; sort?: string; order?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== "" && q.set(k, String(v)))
    return req<PostPage>(`/posts?${q.toString()}`)
  },
  getPostFinders: () => req<{ items: PostFinder[] }>("/posts/finders"),
  getPostsMeta: () => req<{ meta: Record<string, PostMeta> }>("/posts/meta"),
  postsRefresh: (account_id?: string) =>
    req<{ ok: boolean; accounts: string[] }>("/posts/refresh", { method: "POST", body: JSON.stringify({ account_id }) }),
  postsBatch: (action: "hide" | "unhide" | "sticky" | "unsticky", items: { finder_id: string; object_id: string }[]) =>
    req<{ ok: boolean; total: number }>("/posts/batch", { method: "POST", body: JSON.stringify({ action, items }) }),'''
assert s.count(old) == 1, "api posts block"
s = s.replace(old, new, 1)

assert s != orig
io.open(P, "w", encoding="utf-8", newline="").write(s)

chk = [
    ("PostItem finder_id", "  finder_id: string\n  /** 视频号展示名" in s),
    ("PostFinder", "export interface PostFinder {" in s),
    ("getPostFinders", "getPostFinders: () =>" in s),
    ("batch 传 finder_id", "items: { finder_id: string; object_id: string }[]" in s),
]
bad = [n for n, ok in chk if not ok]
print("SELFCHECK", "PASS" if not bad else ("FAIL " + str(bad)))
sys.exit(0 if not bad else 1)
