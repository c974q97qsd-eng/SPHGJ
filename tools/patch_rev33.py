# -*- coding: utf-8 -*-
"""rev33: 「打开」按钮只在本机访问时显示。

非本机(局域网其他电脑 / 手机)通过 WEB 访问时,点「打开」会在母本屏幕上弹窗口,
用户根本看不到,属于误点。所以要:
1. 后端新增 GET /api/system/whoami,判断请求来源是不是「运行程序的这台机器」
   (client.host 命中 127.0.0.1/::1 或本机任一网卡 IPv4)
2. open-window 接口加 403 保护(前端藏了按钮,接口也要拦,双保险)
3. 前端 AccountCard 拉一次 whoami(模块级 Promise 缓存,只发一个请求),
   非本机不渲染「打开」按钮;「远程串流」按钮保留(远程用户就该用它)
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATCHES = {
 "backend/server.py": [
  # 1) 本机判定工具 + whoami 接口(插在 system/info 之前)
  (
'''@app.get("/api/system/info")
async def system_info():''',
'''_LOCAL_IPS = {"ips": None, "ts": 0.0}


def _local_ip_set():
    """本机所有 IPv4 地址(含回环)。缓存 60 秒,网卡热插拔也能跟上。"""
    now = time.time()
    if _LOCAL_IPS["ips"] is not None and now - _LOCAL_IPS["ts"] < 60:
        return _LOCAL_IPS["ips"]
    import socket
    ips = {"127.0.0.1", "::1"}
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ips.add(a.address)
    except Exception:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except Exception:
            pass
    _LOCAL_IPS["ips"], _LOCAL_IPS["ts"] = ips, now
    return ips


def _client_is_local(request):
    """请求是否来自「运行程序的这台机器」。

    判据:client.host in {127.0.0.1, ::1} 或命中本机任一网卡 IPv4。
    本机浏览器用局域网 IP(如 192.168.1.27)访问自己时,TCP 源地址就是本机网卡 IP,
    所以也能正确判为本机;别的电脑访问时源地址是它自己的 IP,判为非本机。
    """
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host in _local_ip_set()


@app.get("/api/system/whoami")
async def system_whoami(request: Request):
    """当前访问者是不是运行程序的这台机器(决定是否显示本机专属功能)。"""
    local = _client_is_local(request)
    return {"local": local,
            "client": (request.client.host if request.client else ""),
            "local_ips": sorted(_local_ip_set()) if local else []}


@app.get("/api/system/info")
async def system_info():''',
   "whoami 接口 + 本机判定"),
  # 2) open-window 加 403 保护
  (
'''@app.post("/api/accounts/{account_id}/open-window")
async def account_open_window(account_id: str):''',
'''@app.post("/api/accounts/{account_id}/open-window")
async def account_open_window(account_id: str, request: Request):''',
   "open-window 加 request 参数"),
  (
'''    acc = next((a for a in config.get("accounts", []) if a["id"] == account_id), None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    pw = await manager._ensure_playwright()''',
'''    # 非本机访问不弹窗口:窗口会出现在运行程序的屏幕上,访问者根本看不到
    if not _client_is_local(request):
        raise HTTPException(403, "本机窗口只能在运行程序的这台机器上打开"
                                 "(远程请用「远程」按钮走网页串流)")
    acc = next((a for a in config.get("accounts", []) if a["id"] == account_id), None)
    if not acc:
        raise HTTPException(404, "账号不存在")
    pw = await manager._ensure_playwright()''',
   "open-window 403 拦截"),
 ],

 "frontend/src/lib/api.ts": [
  (
'''  getLocalWindows: () =>''',
'''  // rev33: 当前访问者是否「运行程序的这台机器」(决定是否显示「打开」按钮)
  whoami: () => req<{ local: boolean; client: string }>("/system/whoami"),
  getLocalWindows: () =>''',
   "api 加 whoami"),
 ],

 "frontend/src/pages/AccountsPage.tsx": [
  (
'''function AccountCard({ acc, onRefresh }: { acc: AccountStatus; onRefresh: () => void }) {
  const { busy, start, stop, openBrowser, remove, setLock, logout } = useAccountActions(onRefresh)
  const [delOpen, setDelOpen] = useState(false)
  const [reloginOpen, setReloginOpen] = useState(false)''',
'''// rev33: 只在「运行程序的这台机器」上显示「打开」按钮。
// 模块级缓存,多张卡片共用同一个请求;失败按非本机处理(宁可少显示也别让人误点)。
let _whoamiPromise: Promise<{ local: boolean }> | null = null
function useIsLocal() {
  const [isLocal, setIsLocal] = useState(false)
  useEffect(() => {
    if (!_whoamiPromise) {
      _whoamiPromise = api.whoami().catch(() => ({ local: false, client: "" }))
    }
    let alive = true
    _whoamiPromise.then((r) => { if (alive) setIsLocal(!!r.local) })
    return () => { alive = false }
  }, [])
  return isLocal
}

function AccountCard({ acc, onRefresh }: { acc: AccountStatus; onRefresh: () => void }) {
  const { busy, start, stop, openBrowser, remove, setLock, logout } = useAccountActions(onRefresh)
  const isLocal = useIsLocal()
  const [delOpen, setDelOpen] = useState(false)
  const [reloginOpen, setReloginOpen] = useState(false)''',
   "卡片加 useIsLocal"),
  (
'''            <Button size="sm" variant="outline" onClick={handleOpen} disabled={isBusy} aria-label="在本机打开账号浏览器" title="在运行程序的这台电脑上直接打开浏览器窗口(不影响后台抓取与直播,5 分钟无操作自动关闭)">{<ExternalLink className="h-3.5 w-3.5" />}打开</Button>''',
'''            {isLocal && (
              <Button size="sm" variant="outline" onClick={handleOpen} disabled={isBusy} aria-label="在本机打开账号浏览器" title="在运行程序的这台电脑上直接打开浏览器窗口(不影响后台抓取与直播,5 分钟无操作自动关闭)">{<ExternalLink className="h-3.5 w-3.5" />}打开</Button>
            )}''',
   "非本机隐藏打开按钮"),
 ],
}


def main():
    for rel, items in PATCHES.items():
        path = os.path.join(ROOT, rel)
        src = io.open(path, encoding="utf-8").read()
        print("== %s ==" % rel)
        for old, new, tag in items:
            n = src.count(old)
            if n != 1:
                raise SystemExit("FAIL 命中 %d 次(需恰好 1): %s -> %s" % (n, tag, rel))
            src = src.replace(old, new, 1)
            print("  OK  %s" % tag)
        io.open(path, "w", encoding="utf-8", newline="").write(src)

    print("\n== 自校验 ==")
    checks = [
        ("backend/server.py", "async def system_whoami"),
        ("backend/server.py", "def _client_is_local(request)"),
        ("backend/server.py", '本机窗口只能在运行程序的这台机器上打开'),
        ("frontend/src/lib/api.ts", "whoami: () => req<{ local: boolean; client: string }>"),
        ("frontend/src/pages/AccountsPage.tsx", "function useIsLocal()"),
        ("frontend/src/pages/AccountsPage.tsx", "{isLocal && ("),
    ]
    bad = 0
    for rel, needle in checks:
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        ok = needle in s
        print("  %s  %-42s %s" % ("PASS" if ok else "FAIL", needle[:42], rel))
        if not ok:
            bad += 1
    print("\nSELFCHECK", "PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


sys.exit(main())
