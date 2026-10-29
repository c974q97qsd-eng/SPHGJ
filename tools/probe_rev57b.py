"""rev57 核对第二弹:live_status / live_info 的完整 postData 原文。

重点看 live_status 的 getLiveStreamContext 里是否也冻了场次 ID —— 如果冻了,
改动 4 的 _fresh_post_data 只改顶层 liveObjectId 是不够的;
另外 _fetch_replay 用 `"liveObjectId" in raw` 做字符串判断,若 live_status 的
getLiveStreamContext 序列化后含该字串,会被误判成 live_info 而遭误改。
"""
import io
import json

SPEC = r"\\Sphgj\sphgj\data\live_api_spec.json"

with io.open(SPEC, encoding="utf-8", errors="replace") as f:
    spec = json.load(f)

for acc in ("uspoloassn2", "uspoloassn8"):
    items = spec.get(acc) or {}
    print("=" * 72)
    print(f"### {acc}")
    print("=" * 72)
    for name in ("live_status", "live_info"):
        s = items.get(name) or {}
        post = s.get("postData") or ""
        print(f"\n---- {name}  url={s.get('url','')[:100]}")
        print(f"     headers={sorted((s.get('headers') or {}))}")
        print(f"     postData len={len(post)}  contains 'liveObjectId' = {'liveObjectId' in post}")
        try:
            o = json.loads(post)
            print("     pretty:")
            for line in json.dumps(o, ensure_ascii=False, indent=2).splitlines():
                print("       " + line)
        except Exception as e:
            print(f"     (非 JSON: {e})  raw={post[:600]}")
