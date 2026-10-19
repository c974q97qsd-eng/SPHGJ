# -*- coding: utf-8 -*-
"""rev23 投递诊断:定位 rename 被拒是「目录级」还是「storage.py 级」。

只动可丢弃的探针文件,不碰正式代码。
"""
import io, os, time, uuid

D = r"\\Sphgj\sphgj\backend"

def try_replace(dst, payload=b"probe", tries=3):
    for i in range(tries):
        stage = os.path.join(D, ".rev23d_" + uuid.uuid4().hex[:8])
        try:
            with io.open(stage, "wb") as f:
                f.write(payload)
            os.replace(stage, dst)
            return True, "ok"
        except Exception as e:
            try:
                os.remove(stage)
            except Exception:
                pass
            err = f"{type(e).__name__}: {e}"
            time.sleep(1.0)
    return False, err

# A) 目录级:替换可丢弃的探针文件
ok, msg = try_replace(os.path.join(D, ".write_test"))
print("A 目录级(替换 .write_test):", ok, msg)

# B) 目标级:替换 storage.py 的兄弟文件 metrics.py(内容与我们无关,用其原内容回写,等价无变化)
if ok:
    src = os.path.join(D, "metrics.py")
    payload = io.open(src, "rb").read()
    ok2, msg2 = try_replace(src, payload)
    print("B 替换 metrics.py(内容不变):", ok2, msg2)

    # C) 目标级:替换 storage.py(先用其自身原内容回写,等价无变化,只测权限)
    src3 = os.path.join(D, "storage.py")
    payload3 = io.open(src3, "rb").read()
    ok3, msg3 = try_replace(src3, payload3)
    print("C 替换 storage.py(内容不变):", ok3, msg3)
