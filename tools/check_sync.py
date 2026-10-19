"""比对本地镜像与母本(UNC)的文件一致性 + 行尾格式,投递前必查。"""
import io
import hashlib

PAIRS = [
    (r"D:\SPHGJ\backend\account_manager.py", r"\\Sphgj\sphgj\backend\account_manager.py"),
    (r"D:\SPHGJ\backend\server.py", r"\\Sphgj\sphgj\backend\server.py"),
    (r"D:\SPHGJ\backend\login_capture.py", r"\\Sphgj\sphgj\backend\login_capture.py"),
    (r"D:\SPHGJ\frontend\src\pages\CommentsPage.tsx", r"\\Sphgj\sphgj\frontend\src\pages\CommentsPage.tsx"),
    (r"D:\SPHGJ\frontend\src\pages\AccountsPage.tsx", r"\\Sphgj\sphgj\frontend\src\pages\AccountsPage.tsx"),
    (r"D:\SPHGJ\frontend\src\pages\AutoReplyPage.tsx", r"\\Sphgj\sphgj\frontend\src\pages\AutoReplyPage.tsx"),
    (r"D:\SPHGJ\frontend\src\pages\AutoDeletePage.tsx", r"\\Sphgj\sphgj\frontend\src\pages\AutoDeletePage.tsx"),
    (r"D:\SPHGJ\frontend\src\lib\api.ts", r"\\Sphgj\sphgj\frontend\src\lib\api.ts"),
    (r"D:\SPHGJ\frontend\src\hooks\useAccounts.ts", r"\\Sphgj\sphgj\frontend\src\hooks\useAccounts.ts"),
    (r"D:\SPHGJ\frontend\src\components\layout\AppShell.tsx", r"\\Sphgj\sphgj\frontend\src\components\layout\AppShell.tsx"),
    (r"D:\SPHGJ\frontend\src\components\accounts\AddAccountDialog.tsx", r"\\Sphgj\sphgj\frontend\src\components\accounts\AddAccountDialog.tsx"),
    (r"D:\SPHGJ\frontend\src\App.tsx", r"\\Sphgj\sphgj\frontend\src\App.tsx"),
]


def stat(p):
    try:
        with io.open(p, "rb") as f:
            d = f.read()
        return len(d), hashlib.md5(d).hexdigest()[:12], d.count(b"\r\n")
    except FileNotFoundError:
        return None
    except Exception as e:
        return ("ERR", str(e), 0)


print("%-46s %-22s %-22s %s" % ("file", "local", "unc", "same?"))
for loc, unc in PAIRS:
    a, b = stat(loc), stat(unc)
    same = "SAME" if (a and b and a[1] == b[1]) else "DIFF"
    name = loc.split("\\")[-1]
    print("%-46s %-22s %-22s %s" % (name, f"{a[0]}B/{a[1]}" if a else "MISSING",
                                    f"{b[0]}B/{b[1]}" if b else "MISSING", same))
    if same == "DIFF" and a and b:
        print("      local size=%s crlf=%d | unc size=%s crlf=%d" % (a[0], a[2], b[0], b[2]))
