# -*- coding: utf-8 -*-
r"""
rev51b: 修正 rev51 的 refspec —— 去掉 --prune

rev51 把 fetch 改成 `"!BRANCH!:refs/remotes/origin/!BRANCH!" --prune`，
dry-run 实测发现这会把 ref 直接删掉：

    $ git fetch origin "master:refs/remotes/origin/master" --prune --dry-run
    From github.com:c974q97qsd-eng/SPHGJ
     - [deleted]         (none)     -> origin/master
       refs/remotes/origin/HEAD will become dangling after refs/remotes/origin/master is deleted

原因：给了显式 refspec 时 git 忽略 config 里的 +refs/heads/*:refs/remotes/origin/*，
--prune 就把「不在本次 refspec 覆盖范围」的跟踪引用判为应删；同一 refspec 内的
因为 dry-run 不落对象也被算成 none -> deleted。总之这个组合不可靠。

顺带实测出原脚本的一个隐藏事实（A 列）：
    git fetch origin "master" --prune --dry-run
      * branch            master     -> FETCH_HEAD
  原写法**根本没有更新 refs/remotes/origin/master**，只写了 FETCH_HEAD 这个临时文件。
  所以一旦 FETCH_HEAD 没写成，本地就彻底没有可用引用 —— 这正是用户报错的根因。

最终写法（C 列，实测正确）：
    git fetch origin "+refs/heads/<branch>:refs/remotes/origin/<branch>"   不带 --prune
  - 显式落远端跟踪引用，不依赖 FETCH_HEAD
  - 加 + 允许非快进（远端 force push 时不至于更新失败）
  - 不带 --prune：一键更新只关心当前分支，清远端已删分支毫无必要，
    而且会误删 origin/main、origin/backup/* 等其他跟踪引用
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(ROOT, "tools", "gen_rev34_bats.py")

PATCHES = [
    # 直连：改成 +refs/heads/<branch>:refs/remotes/origin/<branch>，去掉 --prune
    (
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch origin "!BRANCH!:refs/remotes/origin/!BRANCH!" --prune >nul 2>&1''',
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch origin "+refs/heads/!BRANCH!:refs/remotes/origin/!BRANCH!" >nul 2>&1''',
        "直连 fetch 改 +refs/heads 形式并去掉 --prune",
    ),
    # 镜像：同样
    (
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch "%~1" "%~2:refs/remotes/origin/%~2" --no-tags >nul 2>&1''',
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch "%~1" "+refs/heads/%~2:refs/remotes/origin/%~2" --no-tags >nul 2>&1''',
        "镜像 fetch 改 +refs/heads 形式",
    ),
    # 断言跟着改
    (
        r'''    for key in ["fetch origin \"!BRANCH!:refs/remotes/origin/!BRANCH!\" --prune",''',
        r'''    for key in ["fetch origin \"+refs/heads/!BRANCH!:refs/remotes/origin/!BRANCH!\"",''',
        "断言：关键片段同步",
    ),
    # 再加一条断言，防止 --prune 又被加回来
    (
        r'''    chk(":refs/remotes/origin/" in upd,
        "一键更新.bat 的 fetch 显式落远端跟踪引用")''',
        r'''    chk(":refs/remotes/origin/" in upd,
        "一键更新.bat 的 fetch 显式落远端跟踪引用")
    # rev51b: 显式 refspec 与 --prune 同用会把 ref 删掉（实测），必须组合禁用
    chk("refs/remotes/origin/!BRANCH!\" --prune" not in upd,
        "一键更新.bat 的显式 refspec 未与 --prune 混用")''',
        "断言：禁止 refspec + prune 混用",
    ),
]


def main():
    src = io.open(GEN, encoding="utf-8").read()
    orig = src

    for old, new, tag in PATCHES:
        n = src.count(old)
        if n != 1:
            print("[FAIL] %s: 命中 %d 次（应为 1）" % (tag, n))
            return 1
        src = src.replace(old, new, 1)
        print("[OK] %s" % tag)

    if src == orig:
        print("[FAIL] 没有改动")
        return 1

    io.open(GEN, "w", encoding="utf-8", newline="").write(src)
    print("\n生成器已更新：%+d 字节" % (len(src) - len(orig)))

    chk = io.open(GEN, encoding="utf-8").read()
    must = [
        '"+refs/heads/!BRANCH!:refs/remotes/origin/!BRANCH!"',
        '"+refs/heads/%~2:refs/remotes/origin/%~2"',
        ':have_target',
        'git merge --ff-only "!TARGET!"',
        'git reset --hard "!TARGET!"',
    ]
    bad = [m for m in must if m not in chk]
    # 反例：不能再出现 refspec+prune 混用
    if 'refs/remotes/origin/!BRANCH!" --prune' in chk:
        bad.append("refspec+prune 混用残留")
    print("SELFCHECK", "PASS" if not bad else "FAIL -> %s" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
