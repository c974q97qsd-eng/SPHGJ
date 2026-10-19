import { useCallback, useEffect, useRef, useState } from "react"
import { useSearchParams, useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { toast } from "sonner"
import {
  ArrowLeft, ArrowRight, RotateCw, Home, X, Loader2, Timer, MonitorPlay, WifiOff, AlertTriangle,
} from "lucide-react"

type Meta = {
  account_id: string
  account_name: string
  w: number
  h: number
  url: string
  idle_timeout: number
}

/** 远程同步浏览:把账号浏览器的画面串到这里,鼠标键盘直接操作那一端。
 *
 * 后端用 CDP Page.startScreencast 推 JPEG 帧(二进制),输入走 Input.dispatch*。
 * 5 分钟没有任何操作自动断开并恢复数据抓取 —— 串流期间该账号的 worker 是停的。
 */
export function RemotePage() {
  const [sp] = useSearchParams()
  const navigate = useNavigate()
  const account = sp.get("account") || ""

  const [phase, setPhase] = useState<"connecting" | "live" | "closed" | "error">("connecting")
  const [msg, setMsg] = useState("")
  const [meta, setMeta] = useState<Meta | null>(null)
  const [url, setUrl] = useState("")
  const [urlInput, setUrlInput] = useState("")
  const [idleLeft, setIdleLeft] = useState(300)
  const [fps, setFps] = useState(0)

  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const lastActive = useRef(Date.now())
  const metaRef = useRef<Meta | null>(null)
  const drawing = useRef(false)
  const pending = useRef<Blob | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const idleLimit = useRef(300)
  const dragging = useRef(false)
  const lastMove = useRef(0)
  const frameCount = useRef({ n: 0, t: Date.now() })

  const send = useCallback((o: unknown) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o))
  }, [])

  const touch = useCallback(() => { lastActive.current = Date.now() }, [])

  // ---------------- 画面绘制(只画最新帧,画的过程中来的帧直接丢) ----------------
  const paint = useCallback(() => {
    const blob = pending.current
    pending.current = null
    if (!blob) { drawing.current = false; return }
    const cv = canvasRef.current
    const uri = URL.createObjectURL(blob)
    const img = new Image()
    img.onload = () => {
      if (cv) {
        if (cv.width !== img.width) cv.width = img.width
        if (cv.height !== img.height) cv.height = img.height
        cv.getContext("2d")?.drawImage(img, 0, 0)
      }
      URL.revokeObjectURL(uri)
      const fc = frameCount.current
      fc.n += 1
      const now = Date.now()
      if (now - fc.t > 1000) {
        setFps(Math.round((fc.n * 1000) / (now - fc.t)))
        fc.n = 0; fc.t = now
      }
      if (pending.current) requestAnimationFrame(paint)
      else drawing.current = false
    }
    img.onerror = () => {
      URL.revokeObjectURL(uri)
      drawing.current = false
    }
    img.src = uri
  }, [])

  const onFrame = useCallback((blob: Blob) => {
    pending.current = blob
    if (drawing.current) return
    drawing.current = true
    requestAnimationFrame(paint)
  }, [paint])

  // ---------------- 连接 ----------------
  const connect = useCallback(() => {
    if (!account) {
      setPhase("error")
      setMsg("缺少 account 参数,请从账号管理页点「打开」进入")
      return
    }
    setPhase("connecting")
    setMsg("正在启动浏览器…")
    const proto = location.protocol === "https:" ? "wss:" : "ws:"
    const ws = new WebSocket(
      `${proto}//${location.host}/api/remote/ws?account=${encodeURIComponent(account)}`)
    ws.binaryType = "blob"
    wsRef.current = ws

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        let m: Record<string, unknown>
        try { m = JSON.parse(ev.data) } catch { return }
        if (m.t === "opening") {
          setMsg("正在启动浏览器…")
        } else if (m.t === "hello") {
          const mm = m as unknown as Meta
          metaRef.current = mm
          setMeta(mm)
          idleLimit.current = mm.idle_timeout || 300
          setUrl(mm.url || "")
          setUrlInput(mm.url || "")
          setPhase("live")
          setMsg("")
          lastActive.current = Date.now()
          const cv = canvasRef.current
          if (cv) { cv.width = mm.w; cv.height = mm.h }
          setTimeout(() => inputRef.current?.focus(), 80)
        } else if (m.t === "url") {
          setUrl(String(m.url || ""))
          setUrlInput(String(m.url || ""))
        } else if (m.t === "closed") {
          const reason = String(m.reason || "")
          setPhase("closed")
          setMsg(reason === "idle"
            ? "5 分钟没有操作,已自动断开并恢复数据抓取"
            : reason === "kicked"
              ? "这个账号已在另一个标签页里打开,当前连接被接管"
              : reason === "manual" ? "会话已被手动关闭" : "连接已断开")
          ws.close()
        } else if (m.t === "error") {
          setPhase("error")
          setMsg(String(m.message || "建立会话失败"))
        }
        return
      }
      onFrame(ev.data as Blob)
    }
    ws.onerror = () => {
      setPhase((p) => (p === "live" ? "closed" : "error"))
      setMsg("连接出错(网络断开或被服务端拒绝)")
    }
    ws.onclose = () => {
      setPhase((p) => {
        if (p === "live") { setMsg("连接已断开"); return "closed" }
        if (p === "connecting") { setMsg("连接失败,请确认后端在运行"); return "error" }
        return p
      })
    }
  }, [account, onFrame])

  useEffect(() => {
    connect()
    return () => {
      const ws = wsRef.current
      wsRef.current = null
      try { ws?.close() } catch { /* ignore */ }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account])

  // ---------------- 空闲超时(前端计时 + 后端看门狗双保险) ----------------
  useEffect(() => {
    if (phase !== "live") return
    const t = setInterval(() => {
      const left = Math.max(0, Math.round(idleLimit.current - (Date.now() - lastActive.current) / 1000))
      setIdleLeft(left)
      if (left <= 0) {
        setPhase("closed")
        setMsg("5 分钟没有操作,已自动断开并恢复数据抓取")
        try { wsRef.current?.close() } catch { /* ignore */ }
      }
    }, 500)
    return () => clearInterval(t)
  }, [phase])

  // 保活(不刷新「用户动作」时间,否则空闲超时永远不会触发)
  useEffect(() => {
    if (phase !== "live") return
    const t = setInterval(() => send({ t: "ping" }), 25000)
    return () => clearInterval(t)
  }, [phase, send])

  // ---------------- 坐标换算 ----------------
  const toViewport = (clientX: number, clientY: number) => {
    const cv = canvasRef.current
    const m = metaRef.current
    if (!cv || !m) return { x: 0, y: 0 }
    const r = cv.getBoundingClientRect()
    const x = ((clientX - r.left) / Math.max(1, r.width)) * m.w
    const y = ((clientY - r.top) / Math.max(1, r.height)) * m.h
    return {
      x: Math.max(0, Math.min(m.w, Math.round(x))),
      y: Math.max(0, Math.min(m.h, Math.round(y))),
    }
  }

  const onMouseMove = (e: React.MouseEvent) => {
    if (phase !== "live") return
    const now = Date.now()
    if (now - lastMove.current < 50) return
    lastMove.current = now
    const { x, y } = toViewport(e.clientX, e.clientY)
    touch()
    send({ t: "mouse", kind: dragging.current ? "move" : "move", x, y })
  }

  const onMouseDown = (e: React.MouseEvent) => {
    if (phase !== "live") return
    e.preventDefault()
    inputRef.current?.focus()
    const { x, y } = toViewport(e.clientX, e.clientY)
    dragging.current = true
    touch()
    send({ t: "mouse", kind: "down", x, y, button: e.button === 2 ? "right" : "left" })
  }

  const onMouseUp = (e: React.MouseEvent) => {
    if (phase !== "live") return
    const { x, y } = toViewport(e.clientX, e.clientY)
    dragging.current = false
    touch()
    send({ t: "mouse", kind: "up", x, y, button: e.button === 2 ? "right" : "left" })
  }

  // React 的 onWheel 是 passive,阻止不了页面滚动 -> 用原生监听
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const h = (e: WheelEvent) => {
      e.preventDefault()
      if (phase !== "live") return
      const { x, y } = toViewport(e.clientX, e.clientY)
      touch()
      send({ t: "mouse", kind: "wheel", x, y, dy: e.deltaY, dx: e.deltaX })
    }
    cv.addEventListener("wheel", h, { passive: false })
    return () => cv.removeEventListener("wheel", h)
  }, [phase, send, touch])

  // ---------------- 键盘 ----------------
  const SPECIAL = ["Enter", "Tab", "Backspace", "Escape", "Delete", "ArrowLeft",
    "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (phase !== "live") return
    touch()
    if (SPECIAL.includes(e.key)) {
      e.preventDefault()
      send({ t: "key", name: e.key })
    } else if ((e.ctrlKey || e.metaKey) && (e.key === "v" || e.key === "V")) {
      // 粘贴交给 textarea 的 paste 事件处理
    }
  }

  const onInput = (e: React.FormEvent<HTMLTextAreaElement>) => {
    if (phase !== "live") return
    const ta = e.currentTarget
    const ne = e.nativeEvent as InputEvent
    if (ne.isComposing) return          // 输入法组合中,等 compositionend
    const v = ta.value
    if (v) {
      send({ t: "text", text: v })
      ta.value = ""
      touch()
    }
  }

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const txt = e.clipboardData.getData("text")
    if (txt) {
      e.preventDefault()
      send({ t: "text", text: txt })
      touch()
    }
  }

  const nav = (action: string) => {
    touch()
    send({ t: "nav", action })
  }
  const gotoUrl = () => {
    const u = urlInput.trim()
    if (!u) return
    touch()
    send({ t: "nav", action: "goto", url: u })
  }

  const remaining = (() => {
    const m = Math.floor(idleLeft / 60)
    const s = idleLeft % 60
    return `${m}:${String(s).padStart(2, "0")}`
  })()
  const lowIdle = idleLeft <= 60

  return (
    <div className="fixed inset-0 z-50 flex h-screen flex-col bg-background">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <Button size="sm" variant="ghost" className="gap-1" onClick={() => navigate("/accounts")}>
          <X className="h-4 w-4" />关闭
        </Button>
        <div className="flex items-center gap-1">
          <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => nav("back")}
            disabled={phase !== "live"} aria-label="后退"><ArrowLeft className="h-4 w-4" /></Button>
          <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => nav("forward")}
            disabled={phase !== "live"} aria-label="前进"><ArrowRight className="h-4 w-4" /></Button>
          <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => nav("reload")}
            disabled={phase !== "live"} aria-label="刷新"><RotateCw className="h-4 w-4" /></Button>
          <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => nav("home")}
            disabled={phase !== "live"} aria-label="后台首页"><Home className="h-4 w-4" /></Button>
        </div>
        <Input value={urlInput} onChange={(e) => setUrlInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") gotoUrl() }}
          className="h-8 min-w-[12rem] flex-1 font-mono text-xs" placeholder="视频号后台地址" />
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span className="hidden items-center gap-1 sm:flex">
            <MonitorPlay className="h-3.5 w-3.5" />{meta?.account_name || account}
          </span>
          {phase === "live" && <span>{meta?.w}×{meta?.h} · {fps}fps</span>}
          <span className={lowIdle ? "font-medium text-destructive inline-flex items-center gap-1"
            : "inline-flex items-center gap-1"}>
            <Timer className="h-3.5 w-3.5" />{remaining}
          </span>
        </div>
      </div>

      {/* 画面 */}
      <div className="relative flex flex-1 items-center justify-center overflow-hidden bg-black/90">
        <canvas
          ref={canvasRef}
          className="h-auto max-h-full w-auto max-w-full cursor-default"
          style={{ display: phase === "live" ? "block" : "none" }}
          onMouseMove={onMouseMove}
          onMouseDown={onMouseDown}
          onMouseUp={onMouseUp}
          onContextMenu={(e) => e.preventDefault()}
        />
        {/* 隐藏输入区:承接键盘与输入法,内容立即转发给远端 */}
        <textarea
          ref={inputRef}
          className="pointer-events-none absolute h-px w-px opacity-0"
          onKeyDown={onKeyDown}
          onInput={onInput}
          onPaste={onPaste}
          aria-label="远程键盘输入"
        />

        {phase === "connecting" && (
          <div className="flex flex-col items-center gap-2 text-sm text-white/80">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>{msg || "正在连接…"}</span>
            <span className="text-xs text-white/50">首次连接要启动浏览器,约 10-20 秒</span>
          </div>
        )}

        {phase === "error" && (
          <div className="flex max-w-md flex-col items-center gap-3 px-6 text-center text-sm text-white/85">
            <AlertTriangle className="h-6 w-6 text-amber-400" />
            <span>{msg}</span>
            <div className="flex gap-2">
              <Button size="sm" onClick={connect}>重试</Button>
              <Button size="sm" variant="secondary" onClick={() => navigate("/accounts")}>返回账号管理</Button>
            </div>
          </div>
        )}

        {phase === "closed" && (
          <div className="flex max-w-md flex-col items-center gap-3 px-6 text-center text-sm text-white/85">
            <WifiOff className="h-6 w-6 text-white/60" />
            <span>{msg}</span>
            <div className="flex gap-2">
              <Button size="sm" onClick={connect}>重新连接</Button>
              <Button size="sm" variant="secondary" onClick={() => navigate("/accounts")}>返回账号管理</Button>
            </div>
          </div>
        )}
      </div>

      {/* 状态提示 */}
      <div className="flex items-center justify-between border-t px-3 py-1.5 text-xs text-muted-foreground">
        <span className="truncate" title={url}>{url || "—"}</span>
        <span>
          {phase === "live"
            ? "串流期间该账号的数据抓取已暂停;断开或空闲超时会自动恢复"
            : "未连接"}
        </span>
      </div>
    </div>
  )
}
