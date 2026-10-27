"""rev53 投递:backend/api_client.py(修 self.account 未赋值)。

只投 1 个文件,但仍走 unc_atomic.put_file(两步纯 rename),不做直写覆盖。
投递前做基线校验:归一化 CRLF 后比对母本磁盘版与 git HEAD 版 md5,
不一致就停下让人看(避免把母本上别人的改动覆盖掉)。
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
FILES = ["backend/api_client.py"]


def norm_md5(data):
    """CRLF -> LF 归一后再算 md5。仓库内是 LF、母本磁盘是 CRLF,直接比会假报差异。"""
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest()


def git_head_bytes(rel):
    import subprocess
    r = subprocess.run(["git", "-C", LOCAL, "show", "HEAD:" + rel],
                       capture_output=True)
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
        remote_b = io.open(dst, "rb").read()
        head_b = git_head_bytes(rel)
        print("  %-24s 本地 %d B  母本 %d B" % (rel, len(local_b), len(remote_b)))
        if head_b is None:
            print("    ⚠ git 里没有该文件,跳过基线比对")
            continue
        h_head, h_remote = norm_md5(head_b), norm_md5(remote_b)
        if h_head == h_remote:
            print("    基线一致(归一化后 md5 相同)")
        else:
            print("    ⚠ 母本版本与 git HEAD 不同 —— 可能有别人的改动,需人工确认")
            ok = False
    if not ok:
        print("\n基线不一致,已中止。请先确认母本上的差异内容。")
        return 1

    print("\n=== 2. 投递 ===")
    fail = 0
    for rel in FILES:
        src = os.path.join(LOCAL, rel.replace("/", os.sep))
        dst = os.path.join(REMOTE, rel.replace("/", os.sep))
        try:
            r = put_file(src, dst)
            print("  %-24s -> %s" % (rel, r))
        except Exception as e:
            fail += 1
            print("  %-24s FAILED %r" % (rel, e))
    print("\n失败 %d 个" % fail)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
