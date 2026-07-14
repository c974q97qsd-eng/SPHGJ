import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/** 合并 Tailwind class,去冲突。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** 时间戳 -> 友好字符串。 */
export function fmtTime(ts?: number | string | null): string {
  if (!ts) return "—"
  const d = typeof ts === "number" ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts)
  if (isNaN(d.getTime())) return "—"
  const now = new Date()
  const diff = (now.getTime() - d.getTime()) / 1000
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
}

/** _aid 等敏感字段脱敏。 */
export function mask(s?: string, keep = 4): string {
  if (!s) return "—"
  if (s.length <= keep) return s
  return s.slice(0, keep) + "••••" + s.slice(-2)
}
