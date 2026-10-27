# -*- coding: utf-8 -*-
r"""
rev51: 一键更新.bat 不再依赖隐式的 FETCH_HEAD

问题（用户实测截图）：
    fatal: ambiguous argument 'FETCH_HEAD': unknown revision or path not in the working tree

根因：
    FETCH_HEAD 是 git fetch 顺手写的一个「临时文件」，不是 ref。以下情况它不存在：
      - fetch 被 --prune / 镜像通道 / 网络中断打断
      - 工作区是很老的 zip + git init 装的，从来没 fetch 成功过
      - .git 是从别处拷来的
    脚本却直接 git merge --ff-only FETCH_HEAD / git reset --hard FETCH_HEAD，
    git 把它当 revision 解析 -> ambiguous argument，用户看到的是一句看不懂的报错。

修法（双保险，不依赖隐式文件）：
    1. fetch 时显式落 refspec  ->  refs/remotes/origin/<branch>
    2. :fetch_done 后先验证该 ref 存在；不存在再退回 FETCH_HEAD；
       两个都没有就明确报错并给出可操作提示，而不是抛 git 原始错误
    3. merge / reset 都指向已解析好的 TARGET

这是改「生成器」而非改 bat —— 三个 bat 都由它生成，手改 bat 会在下次
重跑生成器时被覆盖（见 MEMORY.md 铁律）。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN = os.path.join(ROOT, "tools", "gen_rev34_bats.py")

PATCHES = [
    # ---------- 1. 直连通道：显式落 refs/remotes/origin/<branch> ----------
    (
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch origin "!BRANCH!" --prune >nul 2>&1''',
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch origin "!BRANCH!:refs/remotes/origin/!BRANCH!" --prune >nul 2>&1''',
        "直连 fetch 加 refspec",
    ),
    # ---------- 2. 镜像通道：同样显式落 ref ----------
    (
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch "%~1" "%~2" --no-tags >nul 2>&1''',
        r'''git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 fetch "%~1" "%~2:refs/remotes/origin/%~2" --no-tags >nul 2>&1''',
        "镜像 fetch 加 refspec",
    ),
    # ---------- 3. fetch_done：解析 TARGET，替代裸 FETCH_HEAD ----------
    (
        r''':fetch_done
echo   [OK] 已取到最新代码,通道: !VIA!
echo.

REM 镜像通道不会更新 origin\分支 这个本地引用,所以统一用 FETCH_HEAD
git merge --ff-only FETCH_HEAD''',
        r''':fetch_done
echo   [OK] 已取到最新代码,通道: !VIA!
echo.

REM rev51: 不再直接引用 FETCH_HEAD —— 它只是个临时文件,fetch 被打断
REM 或历史上从没成功 fetch 过时就根本不存在,git 会报
REM   fatal: ambiguous argument 'FETCH_HEAD'
REM 现在的做法: fetch 时已显式写到 refs\remotes\origin\分支,优先用它。
set "TARGET=refs/remotes/origin/!BRANCH!"
git rev-parse --verify "!TARGET!" >nul 2>&1
if not errorlevel 1 goto :have_target
REM 老版本 git 或不支持该 refspec 时的兜底:退回临时文件
git rev-parse --verify FETCH_HEAD >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 已经连上了远程,但本地找不到要更新的版本引用
    echo          这说明拉取过程被中途打断了。请重跑一次本脚本。
    echo          如果反复出现: 从云盘下载最新 zip 包解压覆盖到本目录
    echo.
    goto :fail
)
set "TARGET=FETCH_HEAD"

:have_target
git merge --ff-only "!TARGET!"''',
        "fetch_done 解析 TARGET + 兜底校验",
    ),
    # ---------- 4. reset 也跟着用 TARGET ----------
    (
        r'''    git reset --hard FETCH_HEAD >nul''',
        r'''    git reset --hard "!TARGET!" >nul''',
        "reset 用 TARGET",
    ),
    # ---------- 5. 自校验断言：跟着改成新写法 ----------
    (
        r'''    for key in ["fetch origin \"!BRANCH!\" --prune", "git merge --ff-only FETCH_HEAD",''',
        r'''    for key in ["fetch origin \"!BRANCH!:refs/remotes/origin/!BRANCH!\" --prune",
                "git merge --ff-only \"!TARGET!\"", ":have_target",''',
        "自校验：关键片段改新写法",
    ),
    (
        r'''    chk(":try_mirror" in upd and "git reset --hard FETCH_HEAD" in upd,
        "一键更新.bat 含镜像子过程与历史跑偏对齐")''',
        r'''    chk(":try_mirror" in upd and "git reset --hard \"!TARGET!\"" in upd,
        "一键更新.bat 含镜像子过程与历史跑偏对齐")
    # rev51 核心不变量: merge / reset 的实参不能再是裸 FETCH_HEAD
    chk("merge --ff-only FETCH_HEAD" not in upd,
        "一键更新.bat 的 merge 不再用裸 FETCH_HEAD")
    chk("reset --hard FETCH_HEAD" not in upd,
        "一键更新.bat 的 reset 不再用裸 FETCH_HEAD")
    chk(":refs/remotes/origin/" in upd,
        "一键更新.bat 的 fetch 显式落远端跟踪引用")''',
        "自校验：新增不变量断言",
    ),
]


def main():
    src = io.open(GEN, encoding="utf-8").read()
    orig = src

    for old, new, tag in PATCHES:
        n = src.count(old)
        if n != 1:
            print("[FAIL] %s: 命中 %d 次（应为 1），中止" % (tag, n))
            return 1
        src = src.replace(old, new, 1)
        print("[OK] %s" % tag)

    if src == orig:
        print("[FAIL] 没有产生任何改动")
        return 1

    io.open(GEN, "w", encoding="utf-8", newline="").write(src)
    print("\n生成器已更新：%+d 字节" % (len(src) - len(orig)))

    chk = io.open(GEN, encoding="utf-8").read()
    must = [
        'refs/remotes/origin/!BRANCH!',
        ':refs/remotes/origin/%~2',
        ':have_target',
        'git merge --ff-only "!TARGET!"',
        'git reset --hard "!TARGET!"',
    ]
    bad = [m for m in must if m not in chk]
    print("SELFCHECK", "PASS" if not bad else "FAIL -> %s" % bad)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
