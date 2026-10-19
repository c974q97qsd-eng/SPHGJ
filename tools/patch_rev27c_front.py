# -*- coding: utf-8 -*-
"""rev27c: 前端接入 —— 作品播放/复制标题 + 「打开」改为远程同步标签页。"""
import io
import os

LOCAL = r"D:\SPHGJ"
FRONT = os.path.join(LOCAL, r"frontend\src")
PATCHES = []


def add(rel, old, new, tag):
    PATCHES.append((rel, old, new, tag))


# ---------------- api.ts ----------------
add(r"frontend\src\lib\api.ts",
    "export const api = {",
    """export type PostPlayResult = {
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

export const api = {""",
    "api: 类型")

add(r"frontend\src\lib\api.ts",
    """  publishClearTitles: (finder_id?: string, only_used = false) =>""",
    """  // 作品播放:后端会校验 CDN 地址时效,过期就用当前登录态现取
  postPlay: (finder_id: string, object_id: string) =>
    req<PostPlayResult>(`/posts/play?finder_id=${encodeURIComponent(finder_id)}&object_id=${encodeURIComponent(object_id)}`),

  // 远程同步浏览
  remoteSessions: () => req<{ items: RemoteSession[] }>("/remote/sessions"),
  remoteClose: (account: string) =>
    req<{ ok: boolean }>(`/remote/close?account=${encodeURIComponent(account)}`, { method: "POST" }),

  publishClearTitles: (finder_id?: string, only_used = false) =>""",
    "api: 方法")

# ---------------- App.tsx 路由 ----------------
add(r"frontend\src\App.tsx",
    'import { PublishPage } from "@/pages/PublishPage"',
    'import { PublishPage } from "@/pages/PublishPage"\nimport { RemotePage } from "@/pages/RemotePage"',
    "App: 导入")

add(r"frontend\src\App.tsx",
    '                <Route path="/publish" element={<PublishPage />} />',
    '                <Route path="/publish" element={<PublishPage />} />\n                <Route path="/remote" element={<RemotePage />} />',
    "App: 路由")

# ---------------- AccountsPage「打开」 ----------------
add(r"frontend\src\pages\AccountsPage.tsx",
    """  const handleOpen = async () => {
    try {
      await openBrowser(acc.id)
      toast.success("已打开浏览器,关闭后自动恢复抓取")
    } catch (e) {
      toast.error("打开失败:" + (e as Error).message)
    }
  }""",
    """  // rev27: 「打开」不再弹本机窗口,改为把该账号的浏览器串流到新标签页
  // (本机内嵌窗口没有多标签,就在当前窗口打开,同一个页面)
  const handleOpen = () => {
    const target = `/remote?account=${encodeURIComponent(acc.id)}`
    const inWebview = !!(window as unknown as { pywebview?: unknown }).pywebview
    if (inWebview) {
      window.location.assign(target)
      return
    }
    const w = window.open(target, "_blank")
    if (!w) {
      toast.info("浏览器拦截了新标签页,已在当前页打开")
      window.location.assign(target)
    }
  }""",
    "账号卡:打开改为远程标签页")

add(r"frontend\\src\\pages\\AccountsPage.tsx",
    """aria-label="打开账号浏览器">{isBusy ? <Loader2 className="animate-spin" /> : <ExternalLink className="h-3.5 w-3.5" />}打开</Button>""",
    """aria-label="远程打开账号浏览器" title="在新标签页里远程操作这个账号的浏览器(5 分钟无操作自动关闭)">{<ExternalLink className="h-3.5 w-3.5" />}打开</Button>""",
    "账号卡:按钮提示")


# ---------------- PostsPage ----------------
add(r"frontend\src\pages\PostsPage.tsx",
    "  const [items, setItems] = useState<PostItem[]>([])",
    "  const [play, setPlay] = useState<{ fid: string; oid: string; title: string } | null>(null)\n  const [items, setItems] = useState<PostItem[]>([])",
    "作品页:play state")

add(r"frontend\src\pages\PostsPage.tsx",
    'import { Card } from "@/components/ui/card"',
    'import { Card } from "@/components/ui/card"\nimport { PlayDialog } from "@/components/posts/PlayDialog"\nimport { toast } from "sonner"\nimport { Copy, Play } from "lucide-react"',
    "作品页:导入")

add(r"frontend\src\pages\PostsPage.tsx",
    """                        {p.cover_path ? (
                          <img src={p.cover_path} alt="" loading="lazy"
                            className="h-full w-full object-cover"
                            onError={(e) => { (e.target as HTMLImageElement).style.display = "none" }} />
                        ) : null}""",
    """                        {p.cover_path ? (
                          <img src={p.cover_path} alt="" loading="lazy"
                            className="h-full w-full object-cover"
                            onError={(e) => { (e.target as HTMLImageElement).style.display = "none" }} />
                        ) : null}
                        {/* 点封面播放(地址过期后端会现取) */}
                        <button type="button"
                          onClick={() => setPlay({ fid: p.finder_id, oid: p.object_id, title: p.title })}
                          className="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 transition-opacity group-hover:opacity-100"
                          aria-label="播放作品">
                          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-white/90 text-black shadow">
                            <Play className="ml-0.5 h-5 w-5 fill-current" />
                          </span>
                        </button>""",
    "作品页:封面播放按钮")

add(r"frontend\src\pages\PostsPage.tsx",
    """                        <p className="line-clamp-2 min-h-[2.5rem] text-xs leading-5" title={p.title}>
                          {p.title || <span className="text-muted-foreground">(无标题)</span>}
                        </p>""",
    """                        {/* 点标题复制 */}
                        <p className="group/title line-clamp-2 min-h-[2.5rem] cursor-pointer text-xs leading-5 hover:text-primary"
                          title={p.title ? `${p.title}\\n(点击复制)` : "(无标题)"}
                          onClick={() => copyText(p.title)}>
                          {p.title || <span className="text-muted-foreground">(无标题)</span>}
                          {p.title ? (
                            <Copy className="ml-1 inline h-3 w-3 align-[-1px] opacity-0 transition-opacity group-hover/title:opacity-60" />
                          ) : null}
                        </p>""",
    "作品页:标题复制")

add(r"frontend\src\pages\PostsPage.tsx",
    """  return (
    <>
      <Card className="p-3">""",
    """  /** 复制文本。局域网 http 下 navigator.clipboard 不可用(非安全上下文),必须兜底。 */
  const copyText = async (t: string) => {
    if (!t) { toast.info("这条作品没有标题"); return }
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(t)
      } else {
        const ta = document.createElement("textarea")
        ta.value = t
        ta.style.position = "fixed"
        ta.style.opacity = "0"
        document.body.appendChild(ta)
        ta.select()
        document.execCommand("copy")
        document.body.removeChild(ta)
      }
      toast.success("标题已复制")
    } catch {
      toast.error("复制失败,请手动选中标题复制")
    }
  }

  return (
    <>
      <PlayDialog open={!!play} onOpenChange={(v) => { if (!v) setPlay(null) }}
        finderId={play?.fid || ""} objectId={play?.oid || ""} title={play?.title} />
      <Card className="p-3">""",
    "作品页:复制函数与弹窗")


def apply():
    bad = []
    for rel, old, new, tag in PATCHES:
        p = os.path.join(LOCAL, rel.replace("\\\\", "\\"))
        s = io.open(p, encoding="utf-8").read()
        n = s.count(old)
        if n != 1:
            print(f"[FAIL] {tag}: 命中 {n} 次")
            bad.append(tag)
            continue
        io.open(p, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
        print(f"[OK ] {tag}")
    if bad:
        raise SystemExit(f"[FAIL] {bad}")
    print("\n[DONE] rev27c 前端补丁完成(还需加 play state)")


if __name__ == "__main__":
    apply()
