"""rev40b 投递：reset_running_tasks 漏 WHERE 修复上线（发布状态误判）。

⚠️ 文件名带 b：tools/deliver_rev40.py 是历史另一件事（登录默认网页二维码），
本次修复的注释里虽然写「rev40」，但那是任务书给的编号，别覆盖同名脚本。

只投 1 个后端文件，无前端改动（不需要 dist）：
    backend/storage.py  (改：Storage.reset_running_tasks 只更新 running 的 id)

仍走 unc_atomic.put_file（两步纯 rename）。UNC 上删不掉文件，只能 rename 搬走，
所以绝不能用 os.replace / 直写 / MoveFileEx（会被 safe-delete 钩子拦成 WinError 5）。

投递前基线校验：归一化 CRLF 后比对母本磁盘版与 git HEAD 版 md5。
母本必须等于 HEAD（= 上一次投递的状态），否则说明有人改过母本，中止。
"""
import hashlib
import io
import os
import subprocess
import sys

sys.path.insert(0, r"D:\SPHGJ")
sys.path.insert(0, r"D:\SPHGJ\tools")

from unc_atomic import put_file   # noqa: E402

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"
FILES = ["backend/storage.py"]


def norm_md5(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_head_bytes(rel):
    r = subprocess.run(["git", "-C", LOCAL, "show", "HEAD:" + rel], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def main():
    print("=== 1. 基线校验(母本磁盘 vs git HEAD) ===")
    ok = True
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        local_b = io.open(src, "rb").read()
        head_b = git_head_bytes(rel)
        if not os.path.exists(dst):
            if head_b is None:
                print("  %-28s 本地 %6d B  母本无(新增文件)" % (rel, len(local_b)))
            else:
                print("  %-28s 本地 %6d B  母本无(但 HEAD 有 —— 异常!)" % (rel, len(local_b)))
                ok = False
            continue
        remote_b = io.open(dst, "rb").read()
        if head_b is None:
            print("  %-28s 本地 %6d B  母本 %6d B  但 git 无该文件，跳过比对"
                  % (rel, len(local_b), len(remote_b)))
            continue
        same = norm_md5(head_b) == norm_md5(remote_b)
        print("  %-28s 本地 %6d B  母本 %6d B  %s"
              % (rel, len(local_b), len(remote_b), "基线一致" if same else "基线不一致!"))
        if not same:
            ok = False
            a = set(head_b.decode("utf-8", "replace").replace("\r\n", "\n").splitlines())
            b = set(remote_b.decode("utf-8", "replace").replace("\r\n", "\n").splitlines())
            only_remote = [x for x in b - a if x.strip()]
            print("    母本独有非空行 %d 条(前 5 条):" % len(only_remote))
            for line in only_remote[:5]:
                print("      " + line[:110])
    if not ok:
        print("\n基线不一致，已中止。")
        return 1

    print("\n=== 2. 保证目标子目录存在 ===")
    for rel in FILES:
        d = os.path.dirname(os.path.join(REMOTE, rel.replace("/", os.sep)))
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
                print("  已创建 %s" % d)
            except Exception as e:
                print("  创建失败 %s: %r" % (d, e))
                return 1
    print("  (已存在的目录不动)")

    print("\n=== 3. 投递 ===")
    fail = 0
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        try:
            r = put_file(src, dst)
            print("  %-28s -> %s" % (rel, r))
        except Exception as e:
            fail += 1
            print("  %-28s FAILED %r" % (rel, e))
    print("\n投递失败 %d 个" % fail)

    print("\n=== 4. 回读校验 ===")
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        a = io.open(src, "rb").read().replace(b"\r\n", b"\n")
        b = io.open(dst, "rb").read().replace(b"\r\n", b"\n")
        print("  %-28s %s (%d B)" % (rel, "一致" if a == b else "不一致!", len(b)))
        if a != b:
            fail += 1
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
