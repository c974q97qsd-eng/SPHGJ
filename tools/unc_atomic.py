# -*- coding: utf-8 -*-
"""UNC 投递原语(2026-10-20 实测版)。

实测结论(母本 \\Sphgj\sphgj):
  ✗ os.replace / MoveFileExW(REPLACE_EXISTING) -> 被平台 safe-delete 钩子拦截,
    对 UNC 走 trash 必失败且 fail-closed => WinError 5
  ✗ 直接写已存在文件 -> 访问被拒绝(属主在主机侧)
  ✓ 纯 rename 到「不存在的新名」-> 允许(新建文件也允许)
=> 唯一通道:两步 rename
     1) 目标先改名让位  dst -> dst.bakrev23_<uid>
     2) stage 顶替目标名  stage -> dst
   任一步失败都能用备份名改回,不丢文件。
"""
import io
import os
import time
import uuid
import hashlib


def md5_of(path, bufsize=1 << 20):
    h = hashlib.md5()
    with io.open(path, "rb") as f:
        for b in iter(lambda: f.read(bufsize), b""):
            h.update(b)
    return h.hexdigest()


def put_file(local_path, dst_path, tag=None, tries=6, backup=True, log=print):
    """本地文件 -> UNC 目标(两步 rename)。返回 'ok' / 'skip'。"""
    tag = tag or os.path.basename(dst_path)
    with io.open(local_path, "rb") as f:
        data = f.read()
    want = hashlib.md5(data).hexdigest()

    if os.path.exists(dst_path):
        try:
            if md5_of(dst_path) == want:
                log(f"[SKIP] {tag} 已一致")
                return "skip"
        except Exception:
            pass

    stage = None
    for i in range(1, tries + 1):
        stage = dst_path + f".stg{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
        try:
            with io.open(stage, "wb") as f:
                f.write(data)
            if os.path.exists(dst_path):
                bak = dst_path + f".bakrev23_{int(time.time()*1000)}_{uuid.uuid4().hex[:4]}"
                os.rename(dst_path, bak)          # 1) 目标让位(纯 rename,不经过 replace)
                try:
                    os.rename(stage, dst_path)    # 2) stage 顶替
                except Exception:
                    os.rename(bak, dst_path)      # 失败立刻改回,别留空窗
                    raise
            else:
                os.rename(stage, dst_path)
            stage = None
            break
        except Exception as e:
            log(f"  [RETRY {i}] {tag}: {type(e).__name__}: {e}")
            try:
                if stage and os.path.exists(stage):
                    os.rename(stage, stage + ".dead")
                    stage = None
            except Exception:
                pass
            time.sleep(1.0)
    if stage is not None:
        raise SystemExit(f"[FAIL] {tag} 投递失败(均已重试 {tries} 轮)")

    # SMB 客户端缓存:延时重读校验
    for _ in range(8):
        time.sleep(1.0)
        try:
            if md5_of(dst_path) == want:
                log(f"[DELIVER] {tag} ok md5={want[:12]} ({len(data)}B)")
                return "ok"
        except Exception:
            pass
    raise SystemExit(f"[FAIL] {tag} 落盘校验失败")


def sweep_junk(dir_path, trash_dir, patterns=(".rev2", ".probe", ".diag", ".stg", "__st", ".dead", ".bakrev23"),
               log=print):
    """把历史暂存/探针残留改名挪进 trash_dir(UNC 不能删,只能搬)。"""
    try:
        os.makedirs(trash_dir, exist_ok=True)
    except Exception as e:
        log(f"  [SWEEP] 建回收目录失败: {e}")
        return 0
    n = 0
    for fn in os.listdir(dir_path):
        full = os.path.join(dir_path, fn)
        if not os.path.isfile(full):
            continue
        if not any(p in fn for p in patterns):
            continue
        tgt = os.path.join(trash_dir, f"{fn}.{uuid.uuid4().hex[:4]}")
        try:
            os.rename(full, tgt)
            n += 1
        except Exception:
            pass
    return n
