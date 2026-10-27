# -*- coding: utf-8 -*-
r"""
rev51 端到端验证：复现「FETCH_HEAD 不存在」的故障场景

模拟用户那台机器的状态：
    - 本地有个旧版本
    - 从来没成功 fetch 过（或者被打断），FETCH_HEAD 这个文件不存在
    - refs/remotes/origin/master 落后好几个版本

然后对比两种写法：
    旧：git merge --ff-only FETCH_HEAD        -> 应报 ambiguous argument（用户看到的错）
    新：set TARGET=refs/remotes/origin/master
        git merge --ff-only "!TARGET!"        -> 应成功快进

全部在临时目录里做，用 file:// 当远程，不碰 D:\SPHGJ 的 .git。
"""
import os
import shutil
import subprocess
import sys
import tempfile

GIT = r"D:\Program Files\Git\cmd\git.exe"
if not os.path.exists(GIT):
    GIT = shutil.which("git") or "git"

ENV = dict(os.environ)
ENV.update({
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_TERMINAL_PROMPT": "0",
})

results = []


def sh(cwd, *args, check=False):
    p = subprocess.run([GIT] + list(args), cwd=cwd, env=ENV,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError("git %s 失败: %s" % (" ".join(args), p.stderr.strip()))
    return p


def chk(ok, msg):
    results.append((ok, msg))
    print(("  PASS  " if ok else "  FAIL  ") + msg)


def main():
    base = tempfile.mkdtemp(prefix="rev51_")
    remote = os.path.join(base, "remote.git")
    seed = os.path.join(base, "seed")
    user = os.path.join(base, "user")
    try:
        print("[1] 造远程仓库与两个版本")
        sh(base, "init", "--bare", "-b", "master", remote, check=True)
        sh(base, "init", "-b", "master", seed, check=True)
        with open(os.path.join(seed, "app.py"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        sh(seed, "add", "-A", check=True)
        sh(seed, "commit", "-m", "v1", check=True)
        sh(seed, "remote", "add", "origin", remote, check=True)
        sh(seed, "push", "origin", "master", check=True)
        c1 = sh(seed, "rev-parse", "HEAD", check=True).stdout.strip()

        print("[2] 用户机 clone（此时是 v1）")
        sh(base, "clone", remote, user, check=True)
        chk(sh(user, "rev-parse", "HEAD").stdout.strip() == c1, "用户机在 v1")

        print("[3] 远端前进一个版本 v2")
        with open(os.path.join(seed, "app.py"), "w", encoding="utf-8") as f:
            f.write("v2\n")
        sh(seed, "commit", "-am", "v2", check=True)
        sh(seed, "push", "origin", "master", check=True)
        c2 = sh(seed, "rev-parse", "HEAD", check=True).stdout.strip()

        print("[4] 把用户机的 FETCH_HEAD 干掉 + origin/master 回退到 v1")
        fh = os.path.join(user, ".git", "FETCH_HEAD")
        if os.path.exists(fh):
            os.remove(fh)
        # 让 refs/remotes/origin/master 也停在 v1，还原「落后」的真实状态
        sh(user, "update-ref", "refs/remotes/origin/master", c1, check=True)
        has_fh = os.path.exists(fh)
        chk(not has_fh, "FETCH_HEAD 已不存在（模拟故障）")
        chk(sh(user, "rev-parse", "--verify", "FETCH_HEAD").returncode != 0,
            "rev-parse --verify FETCH_HEAD 失败（正是用户遇到的 ambiguous argument）")

        print("[5] 旧写法形态一：FETCH_HEAD 文件不存在")
        old = sh(user, "merge", "--ff-only", "FETCH_HEAD")
        err = (old.stderr or "") + (old.stdout or "")
        chk(old.returncode != 0 and "could not open" in err,
            "旧写法报错(文件不存在): %s" % err.strip().splitlines()[0][:70])

        print("[5b] 旧写法形态二：FETCH_HEAD 存在但为空 -> reset 报 ambiguous argument")
        with open(fh, "w", encoding="utf-8") as f0:
            f0.write("")
        # merge 对空 FETCH_HEAD 只会提示 Already up to date（errorlevel 0），
        # 所以用户的报错一定来自 C 分支里的 reset --hard，这里精确复现它
        old2 = sh(user, "reset", "--hard", "FETCH_HEAD")
        err2 = (old2.stderr or "") + (old2.stdout or "")
        chk(old2.returncode != 0 and "ambiguous argument" in err2,
            "旧写法 reset 报错(空文件): %s" % err2.strip().splitlines()[0][:70])
        os.remove(fh)

        print("[6] 新写法：fetch 显式落 ref -> 解析 TARGET -> merge")
        f = sh(user, "-c", "http.lowSpeedLimit=1000",
               "-c", "http.lowSpeedTime=30",
               "fetch", "origin", "+refs/heads/master:refs/remotes/origin/master")
        chk(f.returncode == 0, "显式 refspec fetch 成功（无 --prune）")
        tgt = sh(user, "rev-parse", "--verify", "refs/remotes/origin/master")
        chk(tgt.returncode == 0, "TARGET ref 可解析")
        chk(tgt.stdout.strip() == c2, "TARGET 指向远端最新 v2")

        # 关键：确认其他远端跟踪引用没被误删
        sh(user, "update-ref", "refs/remotes/origin/keep-me", c1, check=True)
        sh(user, "-c", "http.lowSpeedLimit=1000",
           "fetch", "origin", "+refs/heads/master:refs/remotes/origin/master")
        keep = sh(user, "rev-parse", "--verify", "refs/remotes/origin/keep-me")
        chk(keep.returncode == 0, "其他远端跟踪引用未被误删（无 --prune 生效）")

        m = sh(user, "merge", "--ff-only", "refs/remotes/origin/master")
        chk(m.returncode == 0, "新写法 merge --ff-only 成功")
        chk(sh(user, "rev-parse", "HEAD").stdout.strip() == c2, "用户机已快进到 v2")

        print("[7] 反向验证：带 --prune 的写法会删 ref（所以必须不用）")
        pr = sh(user, "-c", "http.lowSpeedLimit=1000",
                "fetch", "origin", "master:refs/remotes/origin/master",
                "--prune", "--dry-run")
        combo = (pr.stdout or "") + (pr.stderr or "")
        chk("deleted" in combo, "带 --prune 的 refspec 组合确实会删 ref（禁用它有依据）")

        ok = all(r[0] for r in results)
        print("\nRESULT:", "ALL PASS (%d/%d)" % (sum(1 for r in results if r[0]),
                                                 len(results)) if ok else "HAS FAILURE")
        return 0 if ok else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
