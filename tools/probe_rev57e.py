# -*- coding: utf-8 -*-
"""rev57 核对第五弹:看母本 config.json 里账号 dict 的结构(脱敏)。"""
import io
import json

CFG = r"\\Sphgj\sphgj\config.json"
cfg = json.load(io.open(CFG, encoding="utf-8", errors="replace"))
print("top keys:", sorted(cfg))
print("config version:", cfg.get("version"))
accs = cfg.get("accounts") or []
print("accounts 数量:", len(accs))
if accs:
    a = accs[0]
    print("\n账号字段:", sorted(a))
    for k in sorted(a):
        v = a[k]
        if k == "storage_state":
            ss = v or {}
            cks = ss.get("cookies") or []
            names = [c.get("name") for c in cks if c.get("domain", "").lower() in
                     ("channels.weixin.qq.com", "weixin.qq.com", ".weixin.qq.com")]
            print(f"  storage_state: cookies={len(cks)} 与API域相关={len(names)} {sorted(set(n for n in names if n))[:14]}")
        elif isinstance(v, (dict, list)):
            print(f"  {k}: <{type(v).__name__} len={len(v)}>")
        elif k in ("name",):
            print(f"  {k}: {v!r}")
        else:
            s = str(v)
            print(f"  {k}: {(s[:28] + '...') if len(s) > 30 else s!r}")
print("\n其余账号 id / _aid / _log_finder_id:")
for a in accs:
    fid = (a.get("_log_finder_id") or "")[:22]
    print(f"  id={a.get('id'):<14} name={str(a.get('name'))[:14]:<16} "
          f"_aid={'有' if a.get('_aid') else '缺'} finder={fid} "
          f"cookies={len(((a.get('storage_state') or {}).get('cookies') or []))}")
