import { useEffect, useRef, useState } from "react"

/** 728×90 广告位:读 dxzt-v2 服务的 ad_config.json。
 *  - ad_config.json 空 / 无代码 -> 占位(728×90 虚框)
 *  - 填 {html|code: "腾讯联盟广告代码(含 <script>)"} -> 注入并手动执行 script 渲染广告
 *  React 的 dangerouslySetInnerHTML 不执行 <script>,需重建 script 节点才能运行第三方广告 SDK。 */
const AD_URL = "http://122.51.76.84/ad/ad_config.json"

type AdConfig = { enabled?: boolean; html?: string; code?: string }

export function AdSlot() {
  const ref = useRef<HTMLDivElement>(null)
  const [cfg, setCfg] = useState<AdConfig | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    fetch(AD_URL, { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error()
        return r.text()
      })
      .then((txt) => {
        if (!alive) return
        if (!txt.trim()) return  // 空文件 -> 占位
        try { setCfg(JSON.parse(txt)) } catch { setFailed(true) }
      })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false }
  }, [])

  const code = cfg?.html || cfg?.code || ""
  const show = !!code && cfg?.enabled !== false

  useEffect(() => {
    if (!show || !ref.current) return
    const el = ref.current
    el.innerHTML = code
    // 重建 <script> 使其被执行(第三方广告 SDK 依赖)
    el.querySelectorAll("script").forEach((old) => {
      const s = document.createElement("script")
      Array.from(old.attributes).forEach((a) => s.setAttribute(a.name, a.value))
      s.textContent = old.textContent
      old.parentNode?.replaceChild(s, old)
    })
  }, [show, code])

  if (!show) {
    return (
      <div className="flex justify-center px-4 md:px-6 pt-3">
        <div
          className="flex h-[90px] w-[728px] max-w-full items-center justify-center rounded-md border border-dashed border-muted-foreground/30 bg-muted/30 text-xs text-muted-foreground/80"
          role="complementary"
          aria-label="广告位 728 乘 90"
        >
          {failed ? "广告加载失败" : "广告位 728 × 90"}
        </div>
      </div>
    )
  }

  return (
    <div className="flex justify-center px-4 md:px-6 pt-3">
      <div ref={ref} className="min-h-[90px] w-[728px] max-w-full overflow-hidden" role="complementary" aria-label="广告位" />
    </div>
  )
}
