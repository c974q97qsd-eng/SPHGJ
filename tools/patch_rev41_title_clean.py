# -*- coding: utf-8 -*-
"""rev41 接线:标题池「粘贴自动清格式」

1) backend/server.py: 添加标题走 title_clean.split_titles();新增 /publish/titles/preview
2) frontend api.ts:   publishAddTitles 返回解析统计 + 新 publishPreviewTitles
3) TitlesPanel.tsx:   粘贴后实时预览「识别到 N 条 / 已清理 / 跳过」
"""
import io

PATCHES = [
    # ---------------- backend/server.py ----------------
    (
        r"D:\SPHGJ\backend\server.py",
        "from .storage import Storage\nfrom .account_manager import AccountManager",
        "from .storage import Storage\nfrom .account_manager import AccountManager\nfrom .title_clean import split_titles",
        "server-import",
    ),
    (
        r"D:\SPHGJ\backend\server.py",
        '''@app.post("/api/publish/titles")
async def publish_add_titles(body: dict):
    finder_id = (body.get("finder_id") or "").strip()
    raw = body.get("contents")
    if isinstance(raw, str):
        contents = raw.splitlines()
    elif isinstance(raw, list):
        contents = [str(x) for x in raw]
    else:
        raise HTTPException(400, "contents 需要是数组或换行文本")
    n = storage.add_titles(contents, finder_id=finder_id)
    filled = publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "added": n, "titles_filled": filled}''',
        '''@app.post("/api/publish/titles/preview")
async def publish_preview_titles(body: dict):
    """添加前预览:粘贴文本 -> 清理后的标题列表(不落库)。

    前端据此显示「识别到 N 条 / 已自动清理 / 跳过重复空行」,
    让用户加之前就知道序号、引号、全角空格那些格式会被去掉。
    """
    raw = body.get("contents")
    if not isinstance(raw, (str, list)):
        raise HTTPException(400, "contents 需要是数组或换行文本")
    contents, stat = split_titles(raw)
    return {"ok": True, "items": contents, "count": len(contents),
            "cleaned": stat["cleaned"], "dropped": stat["empty"] + stat["dup"]}


@app.post("/api/publish/titles")
async def publish_add_titles(body: dict):
    finder_id = (body.get("finder_id") or "").strip()
    raw = body.get("contents")
    if not isinstance(raw, (str, list)):
        raise HTTPException(400, "contents 需要是数组或换行文本")
    # rev41: 粘贴文本先清格式(行首序号 / 引号 / 全角空格 / 整段粘成一行),
    # 只清格式不动文案,话题和标点原样保留
    contents, stat = split_titles(raw)
    if not contents:
        raise HTTPException(400, "没识别到有效标题(粘贴的内容只有序号或空行?)")
    n = storage.add_titles(contents, finder_id=finder_id)
    filled = publish_queue.fill_missing_titles()
    await publish_queue._push()
    return {"ok": True, "added": n, "titles_filled": filled,
            "parsed": len(contents), "cleaned": stat["cleaned"],
            "dropped": stat["empty"] + stat["dup"]}''',
        "server-endpoint",
    ),
    # ---------------- frontend/src/lib/api.ts ----------------
    (
        r"D:\SPHGJ\frontend\src\lib\api.ts",
        '''  publishAddTitles: (finder_id: string, contents: string) =>
    req<{ ok: boolean; added: number; titles_filled: number }>("/publish/titles", { method: "POST", body: JSON.stringify({ finder_id, contents }) }),''',
        '''  publishAddTitles: (finder_id: string, contents: string) =>
    req<{ ok: boolean; added: number; titles_filled: number; parsed?: number; cleaned?: number; dropped?: number }>(
      "/publish/titles", { method: "POST", body: JSON.stringify({ finder_id, contents }) }),
  /** 粘贴预览:清理掉行首序号/引号/全角空格后的标题列表(不落库) */
  publishPreviewTitles: (finder_id: string, contents: string) =>
    req<{ ok: boolean; items: string[]; count: number; cleaned: number; dropped: number }>(
      "/publish/titles/preview", { method: "POST", body: JSON.stringify({ finder_id, contents }) }),''',
        "api-ts",
    ),
    # ---------------- TitlesPanel.tsx:状态 ----------------
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)''',
        '''  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // 粘贴预览:后端清理后的真实结果(序号/引号/全角空格已去掉)
  const [preview, setPreview] = useState<{ items: string[]; cleaned: number; dropped: number } | null>(null)''',
        "panel-state",
    ),
    # ---------------- TitlesPanel.tsx:添加逻辑 ----------------
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''  const add = async () => {
    const lines = draft.split("\\n").map((x) => x.trim()).filter(Boolean)
    if (!lines.length) { toast.error("先粘贴标题,一行一条"); return }
    setSaving(true)
    try {
      const r = await api.publishAddTitles(pool, draft)
      toast.success(`添加 ${r.added} 条` + (r.added < lines.length ? `,${lines.length - r.added} 条重复已跳过` : "") +
        (r.titles_filled ? `,已给 ${r.titles_filled} 条待发任务补上标题` : ""))
      setDraft("")
      await load(pool)
    } catch (e) { toast.error((e as Error).message) } finally { setSaving(false) }
  }''',
        '''  // 粘贴/输入后 350ms 问一次后端清理结果(规则只在后端一份,避免两边不一致)
  useEffect(() => {
    const text = draft
    if (!text.trim()) { setPreview(null); return }
    const t = setTimeout(() => {
      api.publishPreviewTitles(pool, text)
        .then((r) => setPreview({ items: r.items, cleaned: r.cleaned, dropped: r.dropped }))
        .catch(() => setPreview(null))
    }, 350)
    return () => clearTimeout(t)
  }, [draft, pool])

  const add = async () => {
    if (!draft.trim()) { toast.error("先粘贴标题"); return }
    setSaving(true)
    try {
      const r = await api.publishAddTitles(pool, draft)
      const bits = [`添加 ${r.added} 条`]
      if (r.cleaned) bits.push(`已自动清理 ${r.cleaned} 条的序号/引号等格式`)
      if (r.dropped) bits.push(`跳过 ${r.dropped} 条空行/重复`)
      if (r.added < (r.parsed ?? r.added)) bits.push(`池内已有 ${(r.parsed ?? r.added) - r.added} 条相同标题`)
      if (r.titles_filled) bits.push(`已给 ${r.titles_filled} 条待发任务补上标题`)
      toast.success(bits.join(","))
      setDraft("")
      setPreview(null)
      await load(pool)
    } catch (e) { toast.error((e as Error).message) } finally { setSaving(false) }
  }''',
        "panel-add",
    ),
    # ---------------- TitlesPanel.tsx:预览 UI ----------------
    (
        r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
        '''              placeholder={"一行一条标题,可批量粘贴。例如:\\n秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\\n这双鞋的配色比图片还好看 #穿搭"}
              className="min-h-[92px] text-sm"
            />
            <Button className="h-auto gap-1 sm:w-28" onClick={add} disabled={saving}>
              <Plus className="h-4 w-4" />添加
            </Button>
          </div>''',
        '''              placeholder={"直接粘贴即可,行首序号、引号、全角空格会自动清掉。例如:\\n1. 秋冬新款男鞋 上脚真的很稳 #男鞋 #老爹鞋\\n2. 这双鞋的配色比图片还好看 #穿搭"}
              className="min-h-[92px] text-sm"
            />
            <Button className="h-auto gap-1 sm:w-28" onClick={add} disabled={saving}>
              <Plus className="h-4 w-4" />添加
            </Button>
          </div>

          {/* 粘贴预览:让用户加之前就看到清理结果,不用赌 */}
          {preview && (
            <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-medium">识别到 {preview.items.length} 条标题</span>
                {preview.cleaned > 0 && <span className="text-muted-foreground">· 已自动清理 {preview.cleaned} 条的序号/引号/全角空格</span>}
                {preview.dropped > 0 && <span className="text-muted-foreground">· 跳过 {preview.dropped} 条空行/重复</span>}
                {!preview.items.length && <span className="text-destructive">· 没有有效内容</span>}
              </div>
              {preview.items.length > 0 && (
                <ol className="mt-1.5 max-h-32 space-y-0.5 overflow-y-auto text-muted-foreground">
                  {preview.items.map((t, i) => (
                    <li key={i} className="truncate">{i + 1}. {t}</li>
                  ))}
                </ol>
              )}
            </div>
          )}''',
        "panel-preview-ui",
    ),
]

SELFCHECK = [
    (r"D:\SPHGJ\backend\server.py",
     ["from .title_clean import split_titles", "/api/publish/titles/preview", "contents, stat = split_titles(raw)"], []),
    (r"D:\SPHGJ\frontend\src\lib\api.ts", ["publishPreviewTitles"], []),
    (r"D:\SPHGJ\frontend\src\components\publish\TitlesPanel.tsx",
     ["publishPreviewTitles", "识别到 {preview.items.length} 条标题"], ["raw.split"]),
]

ok = True
for path, old, new, tag in PATCHES:
    s = io.open(path, encoding="utf-8").read()
    n = s.count(old)
    if n != 1:
        print(f"[FAIL] {tag}: 命中 {n} 次,应为 1 -> {path}")
        ok = False
        continue
    io.open(path, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
    print(f"[ok] {tag}")

print("\n--- 自校验 ---")
for path, must, mustnot in SELFCHECK:
    s = io.open(path, encoding="utf-8").read()
    for k in must:
        good = s.count(k) >= 1
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 含 {k}")
        ok &= good
    for k in mustnot:
        good = s.count(k) == 0
        print(("  PASS " if good else "  FAIL ") + f"{path.split(chr(92))[-1]}: 已无 {k}")
        ok &= good

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURE")
