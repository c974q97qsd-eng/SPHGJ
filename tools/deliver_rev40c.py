"""rev40c 投递：need_review 历史误标「启动自动修复」上线。

投递 3 个文件，无前端改动（不需要 dist）：
    backend/publish_fixups.py     (新增：判定 + 回滚逻辑，启动时调用)
    backend/server.py             (改：import + reset 之后调用)
    tools/fix_rev40_need_review.py(改：改为复用 publish_fixups 的薄 CLI)

基线说明：母本 server.py 里已有上一轮 rev56d（抖音登录）的 178 行改动、尚未入库，
所以**不做「母本 == git HEAD」的强校验**，改为人工核对「母本 -> 本地」的差异
（已核对：只有本任务新增的 17 行 rev40 接线）。
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
FILES = ["backend/publish_fixups.py", "backend/server.py",
         "tools/fix_rev40_need_review.py"]


def norm_md5(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_head_bytes(rel):
    r = subprocess.run(["git", "-C", LOCAL, "show", "HEAD:" + rel], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def main():
    print("=== 1. 基线提示(母本 vs git HEAD，仅提示不中止) ===")
    for rel in FILES:
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        head_b = git_head_bytes(rel)
        if not os.path.exists(dst):
            print("  %-32s 母本无此文件（按新增投递）" % rel)
            continue
        remote_b = io.open(dst, "rb").read()
        if head_b is None:
            print("  %-32s 母本 %6d B  git 无该文件" % (rel, len(remote_b)))
        else:
            same = norm_md5(head_b) == norm_md5(remote_b)
            print("  %-32s 母本 %6d B  HEAD %6d B  %s"
                  % (rel, len(remote_b), len(head_b), "一致" if same else "不一致(预期内,见 docstring)"))

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
