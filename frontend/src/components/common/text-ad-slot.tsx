import { useEffect, useState } from "react"

/** 文字滚动广告位:读 dxzt-v2 服务的 ad_text_ads.json,按 interval_ms 轮播,
 *  上滑过渡。放 AppShell header 中间(标题后、启动按钮前)。 */
const AD_URL = "http://122.51.76.84/ad/ad_text_ads.json"

type AdData = { interval_ms?: number; ad_list?: string[] }

function extractUrl(text: string): { label: string; url: string | null } {
  const m = text.match(/https?:\/\/\S+/)
  if (!m) return { label: text, url: null }
  const url = m[0]
  return { label: text.replace(url, "").trim() || url, url }
}

export function TextAdSlot() {
  const [data, setData] = useState<AdData | null>(null)
  const [idx, setIdx] = useState(0)

  useEffect(() => {
    let alive = true
    fetch(AD_URL, { cache: "no-store" })
      .then((r) => r.json())
      .then((d: AdData) => { if (alive) setData(d) })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  const list = (data?.ad_list || []).map((s) => s.trim()).filter(Boolean)
  useEffect(() => {
    if (list.length <= 1) return
    const ms = Math.max(1000, data?.interval_ms || 3000)
    const t = setInterval(() => setIdx((i) => (i + 1) % list.length), ms)
    return () => clearInterval(t)
  }, [list.length, data?.interval_ms])

  if (list.length === 0) return null
  const { label, url } = extractUrl(list[idx] || list[0])

  return (
    <div className="mx-4 hidden min-w-0 max-w-md flex-1 items-center justify-center md:flex">
      <div key={idx} className="animate-slide-up truncate text-sm text-muted-foreground">
        {url ? (
          <a href={url} target="_blank" rel="noopener noreferrer" className="transition-colors hover:text-foreground">
            {label}
          </a>
        ) : (
          <span>{label}</span>
        )}
      </div>
    </div>
  )
}
