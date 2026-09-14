/** 界面缩放(DPI 档位):大 / 中 / 小。
 *
 * 实现:只改 <html> 的 font-size。全站尺寸走 Tailwind 的 rem 体系
 * (h-9 / p-4 / text-sm 等),因此改根字号即可整体等比缩放,不需要动组件。
 * 档位:大=16px(100%,原始) / 中=14px(约 88%) / 小=12px(75%)。
 *
 * 持久化双写:
 *  - localStorage:立即生效,配合 index.html 内联脚本做到首屏无闪烁;
 *  - config.json:随配置备份/迁移,换机器重装后仍保留选择。
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react"
import { api } from "@/lib/api"

export type UiScale = "large" | "medium" | "small"

export const UI_SCALE_OPTIONS: {
  value: UiScale
  label: string
  rootPx: number
  hint: string
}[] = [
  { value: "large", label: "大", rootPx: 16, hint: "100%" },
  { value: "medium", label: "中", rootPx: 14, hint: "88%" },
  { value: "small", label: "小", rootPx: 12, hint: "75%" },
]

export const STORAGE_KEY = "sphgj.ui_scale"

/** 首次使用默认「中」:1080P 屏上「大」会显得撑满,中档一屏能看更多内容。 */
export const DEFAULT_UI_SCALE: UiScale = "medium"

export function isUiScale(v: unknown): v is UiScale {
  return v === "large" || v === "medium" || v === "small"
}

/** 立刻把档位应用到根元素(index.html 内联脚本与 Provider 共用同一逻辑)。 */
export function applyUiScale(v: UiScale) {
  if (typeof document === "undefined") return
  const opt = UI_SCALE_OPTIONS.find((o) => o.value === v) ?? UI_SCALE_OPTIONS[1]
  const el = document.documentElement
  el.style.fontSize = `${opt.rootPx}px`
  el.dataset.uiScale = opt.value
}

export function readLocalUiScale(): UiScale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return isUiScale(v) ? v : null
  } catch {
    return null
  }
}

interface UiScaleCtxValue {
  scale: UiScale
  setScale: (v: UiScale) => void
}

const UiScaleCtx = createContext<UiScaleCtxValue>({
  scale: DEFAULT_UI_SCALE,
  setScale: () => {},
})

export function UiScaleProvider({ children }: { children: ReactNode }) {
  const [scale, setScaleState] = useState<UiScale>(() => readLocalUiScale() ?? DEFAULT_UI_SCALE)

  useEffect(() => {
    applyUiScale(scale)
  }, [scale])

  // 与后端 config 对齐:本机没选过、但配置里有(换机/重装)时以配置为准
  useEffect(() => {
    let alive = true
    api.getConfig().then((c) => {
      if (!alive) return
      const remote = (c as { ui_scale?: unknown }).ui_scale
      if (isUiScale(remote) && remote !== readLocalUiScale()) {
        try { localStorage.setItem(STORAGE_KEY, remote) } catch { /* 隐私模式下忽略 */ }
        setScaleState(remote)
      }
    }).catch(() => { /* 后端不可用就用本地值 */ })
    return () => { alive = false }
  }, [])

  const setScale = useCallback((v: UiScale) => {
    setScaleState(v)
    try { localStorage.setItem(STORAGE_KEY, v) } catch { /* ignore */ }
    // 同步到 config.json;写失败不影响本机立即生效
    api.patchConfig({ ui_scale: v }).catch(() => {})
  }, [])

  const value = useMemo(() => ({ scale, setScale }), [scale, setScale])
  return <UiScaleCtx.Provider value={value}>{children}</UiScaleCtx.Provider>
}

export function useUiScale() {
  return useContext(UiScaleCtx)
}
