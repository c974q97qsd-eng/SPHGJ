# -*- coding: utf-8 -*-
"""诊断:UNC 上「改名到不存在的新名」是否被允许(这是无替换语义的纯 rename)。

如果允许 -> 投递可拆成两步纯 rename:目标先改名让位,再把 stage 改成目标名。
"""
import io, os, time, uuid

D = r"\\Sphgj\\sphgj\\backend".replace("\\\\", "\\")
D = r"\\Sphgj\sphgj\backend"

def u(n):
    return os.path.join(D, n + "_" + uuid.uuid4().hex[:6])

# 1) 新文件 -> 新文件(目标不存在)
a, b = u(".diagA"), u(".diagB")
io.open(a, "wb").write(b"x")
try:
    os.rename(a, b)
    print("[1] 新建->不存在的新名 rename:", "OK")
except Exception as e:
    print("[1] 失败:", type(e).__name__, e)

# 2) storage.py -> 不存在的新名(测 DELETE_CHILD / 源删除权限)
src = os.path.join(D, "storage.py")
dst = u("storage.py.bakdiag")
try:
    os.rename(src, dst)
    print("[2] storage.py 改名让位: OK")
    back_ok = False
    for _ in range(5):
        try:
            os.rename(dst, src)
            back_ok = True
            break
        except Exception as e:
            print("    改回重试:", type(e).__name__, e)
            time.sleep(1)
    print("    改回:", "OK" if back_ok else "★失败,storage.py 现在叫 " + os.path.basename(dst))
except Exception as e:
    print("[2] 失败:", type(e).__name__, e)

# 3) 目标已存在时 os.rename(应该 WinError 183,不触发钩子)
c = u(".diagC")
io.open(c, "wb").write(b"y")
try:
    os.rename(c, os.path.join(D, ".write_test"))
    print("[3] rename 到已存在目标: OK(意外)")
except Exception as e:
    print("[3] rename 到已存在目标:", type(e).__name__, e)
