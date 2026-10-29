"""rev57 诊断核对:读母本 live_api_spec.json + live_probe/*.json,核对四条根因。

只读,不写任何文件。
"""
import io
import json
import os

SPEC = r"\\Sphgj\sphgj\data\live_api_spec.json"
PROBE = r"\\Sphgj\sphgj\data\live_probe"


def jopen(p):
    with io.open(p, encoding="utf-8", errors="replace") as f:
        return json.load(f)


print("=" * 70)
print("[1] live_api_spec.json —— 缓存的 postData 里 liveObjectId 是什么")
print("=" * 70)
spec = jopen(SPEC)
for acc, items in spec.items():
    print(f"\n-- {acc}  (learned: {sorted(items)})")
    for name, s in items.items():
        post = s.get("postData") or ""
        try:
            o = json.loads(post)
        except Exception:
            print(f"   {name}: postData 非 JSON, len={len(post)}")
            continue
        oid = o.get("liveObjectId", "<无此字段>")
        print(f"   {name}: liveObjectId={oid!r}  keys={sorted(o)[:10]}")

print()
print("=" * 70)
print("[2] live_probe/*.json —— check_live_status 实际返回了什么")
print("=" * 70)
for fn in sorted(os.listdir(PROBE)):
    if not fn.endswith(".json") or fn.endswith("_live_http.json"):
        continue
    p = os.path.join(PROBE, fn)
    try:
        j = jopen(p)
    except Exception as e:
        print(f"\n-- {fn}: 读失败 {e}")
        continue
    d = j.get("data") or {}
    print(f"\n-- {fn}  errCode={j.get('errCode')}")
    print(f"   liveObjectId = {d.get('liveObjectId')!r}")
    print(f"   status       = {d.get('status')!r}   createTime={d.get('createTime')!r}")
    print(f"   liveStats    = {'有' if d.get('liveStats') else '无'}")
    print(f"   liveInfo     = {'有' if d.get('liveInfo') else '无'}")
    print(f"   data_keys({len(d)}) = {sorted(d)}")

print()
print("=" * 70)
print("[3] 对比:缓存的 liveObjectId vs 探针里的实际 liveObjectId")
print("=" * 70)
# 探针文件名 -> 账号（uspoloassn2.json 对应 account_id 常见为 finder id）
for acc, items in spec.items():
    info = items.get("live_info") or {}
    try:
        o = json.loads(info.get("postData") or "{}")
    except Exception:
        o = {}
    cached = str(o.get("liveObjectId") or "")
    hits = []
    for fn in sorted(os.listdir(PROBE)):
        if not fn.endswith(".json") or fn.endswith("_live_http.json"):
            continue
        try:
            d = jopen(os.path.join(PROBE, fn)).get("data") or {}
        except Exception:
            continue
        real = str(d.get("liveObjectId") or "")
        if real:
            hits.append(f"{fn}={real}")
    same = "?" if not cached else ("冻结ID无任何探针匹配" if not any(cached in h for h in hits) else "有匹配")
    print(f"\n{acc}: cached_liveObjectId={cached!r}  -> {same}")
    for h in hits:
        print(f"    探针 {h}")
