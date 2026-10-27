"""rev56 投递:platforms.py + douyin/ 适配层 + storage.py(platform 列)。

只投 2 个后端文件,无前端改动(不需要 dist)。仍走 unc_atomic.put_file(两步纯 rename)。
投递前做基线校验:归一化 CRLF 后比对母本磁盘版与 git HEAD 版 md5。
"""
import hashlib
import io
import os
import sys

sys.path.insert(0, r"D:\SPHGJ")
sys.path.insert(0, r"D:\SPHGJ\tools")

from unc_atomic import put_file   # noqa: E402

LOCAL = r"D:\SPHGJ"
REMOTE = r"\\Sphgj\sphgj"
FILES = ["backend/platforms.py", "backend/douyin/__init__.py",
         "backend/douyin/api.py", "backend/douyin/client.py",
         "backend/storage.py"]


def norm_md5(data):
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_head_bytes(rel):
    import subprocess
    r = subprocess.run(["git", "-C", LOCAL, "show", "HEAD:" + rel], capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout


def main():
    print("=== 1. 基线校验(母本磁盘 vs git HEAD) ===")
    ok = True
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        local_b = io.open(src, "rb").read()
        if not os.path.exists(dst):
            print("  %-32s 本地 %d B  母本无此文件(本次新增,跳过基线比对)" % (rel, len(local_b)))
            continue
        remote_b = io.open(dst, "rb").read()
        head_b = git_head_bytes(rel)
        print("  %-32s 本地 %d B  母本 %d B" % (rel, len(local_b), len(remote_b)))
        if head_b is None:
            print("    ⚠ git 里没有该文件,跳过基线比对")
            continue
        if norm_md5(head_b) == norm_md5(remote_b):
            print("    基线一致(归一化后 md5 相同)")
        else:
            print("    ⚠ 母本版本与 git HEAD 不同 —— 可能有别人的改动,需人工确认")
            # 打印母本独有行数,便于判断是不是严格子集
            try:
                a = set(head_b.decode("utf-8", "replace").replace("\r\n", "\n").splitlines())
                b = set(remote_b.decode("utf-8", "replace").replace("\r\n", "\n").splitlines())
                only_remote = [x for x in b - a if x.strip()]
                print("    母本独有非空行 %d 条(前 5 条):" % len(only_remote))
                for line in only_remote[:5]:
                    print("      " + line[:110])
            except Exception as e:
                print("    差异分析失败: %r" % e)
            ok = False
    if not ok:
        print("\n基线不一致,已中止。")
        return 1

    print("\n=== 2. 保证目标子目录存在(douyin/ 是新包) ===")
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
            print("  %-32s -> %s" % (rel, r))
        except Exception as e:
            fail += 1
            print("  %-32s FAILED %r" % (rel, e))
    print("\n失败 %d 个" % fail)

    print("\n=== 4. 回读校验 ===")
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        a = io.open(src, "rb").read().replace(b"\r\n", b"\n")
        b = io.open(dst, "rb").read().replace(b"\r\n", b"\n")
        print("  %-32s %s (%d B)" % (rel, "一致" if a == b else "不一致!", len(b)))
        if a != b:
            fail += 1
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
