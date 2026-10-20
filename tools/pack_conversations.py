# -*- coding: utf-8 -*-
"""把本项目(SPHGJ)的对话记录 + 项目记忆打包成一个 zip,便于上传云盘后换机继续。

包含:三个工作区的全部会话 jsonl(不含 tool-results 缓存)+ 项目记忆 md + 恢复指引
不包含:代码(走 git)、运行数据(data/*.db、config.json、profiles/ 登录态)
"""
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJ = r"C:\Users\Administrator\.workbuddy\projects"
WS = r"C:\Users\Administrator\WorkBuddy\2026-08-18-00-01-02"
MEM = os.path.join(WS, ".workbuddy", "memory")
OUT_DIR = r"D:\SPHGJ\data\_cloud_sync"

# (本地目录, zip 内目录名, 说明)
SOURCES = [
    (os.path.join(PROJ, "c-Users-Administrator-WorkBuddy-2026-08-18-00-01-02"),
     "conversations/01-main-workspace(C-WorkBuddy-2026-08-18-00-01-02)",
     "当前主工作区(8/18 起,含 rev20~rev27 全部改造过程)"),
    (os.path.join(PROJ, "d-SPHGJ"),
     "conversations/02-d-SPHGJ(D-SPHGJ)",
     "以 D:\\SPHGJ 为工作区的历史会话(8/30~9/15)"),
    (os.path.join(PROJ, "c-Users-Administrator-Desktop-fsdownload-SPHGJ"),
     "conversations/03-desktop-fsdownload-SPHGJ",
     "以 Desktop\\fsdownload\\SPHGJ 为工作区的早期会话"),
]

README = """# SPHGJ 项目 —— 会话记录 + 项目记忆 备份

打包时间: {now}
来源主机: Administrator@本机 (项目代码 D:\\SPHGJ,运行母本 \\\\Sphgj\\sphgj = 192.168.1.27:D:\\SPHGJ)

## 一、包里有什么

```
conversations/01-main-workspace(C-WorkBuddy-2026-08-18-00-01-02)/   {n1} 个会话
conversations/02-d-SPHGJ(D-SPHGJ)/                                  {n2} 个会话
conversations/03-desktop-fsdownload-SPHGJ/                          {n3} 个会话
memory/                                                             项目长期记忆 + 每日工作日志
automations/                                                        自动化配置(若有)
README-恢复指引.md                                                  本文件
```

会话文件是 WorkBuddy 的 JSONL 格式:一行一条消息/工具调用,可用任何编辑器打开,
也可以直接让助手读取它来接续上下文。tool-results/ 渲染缓存没有打包(纯临时产物,占 90% 体积)。

## 二、包里【没有】什么(重要)

| 东西 | 为什么不打包 | 新主机怎么拿 |
|---|---|---|
| 代码 | 走 git 就够了 | `git clone git@github.com:c974q97qsd-eng/SPHGJ.git`(默认分支 master) |
| data/*.db 数据库 | 含全部生产数据,体积大 | 从老主机直接拷 data 整个目录 |
| config.json | 含账号卡片 / 登录态 / 密钥 | 从老主机直接拷 |
| profiles/ | 视频号扫码登录态(cookie) | 从老主机直接拷,重登一次也行 |
| data/covers、publish_inbox | 素材与封面缓存 | 可拷可不拷,封面会重新下载 |

**想真正在新主机跑起来,data 目录 + config.json + profiles 必须一起拷。**
最省事的做法: 老主机上把 D:\\SPHGJ 整个目录打包拷走,再用 git 覆盖一次代码。

## 三、新主机恢复步骤

1. **代码**
   ```
   git clone git@github.com:c974q97qsd-eng/SPHGJ.git D:\\SPHGJ
   ```
   新主机需要配好 GitHub SSH key(`c974q97qsd-eng` 账号)。

2. **运行数据**(从老主机拷)
   `data\\`(库 + covers + publish_inbox)、`config.json`、`profiles\\`

3. **会话记录**(想在新主机的 WorkBuddy 会话列表里看到历史)
   解压到 `C:\\Users\\<新主机用户名>\\.workbuddy\\projects\\<编码目录名>\\`
   目录名编码规则: 工作区绝对路径 → 盘符小写、冒号去掉、`\\` 换成 `-`
   - `D:\\SPHGJ` → `d-SPHGJ`
   - `C:\\Users\\<用户>\\WorkBuddy\\2026-08-18-00-01-02` → `c-Users-<用户>-WorkBuddy-2026-08-18-00-01-02`
   注意用户名不同的话目录名要相应改。

4. **项目记忆**
   把 `memory/` 里的 md 拷到新工作区的 `.workbuddy\\memory\\` 下(`MEMORY.md` + 日期日志)。

5. **启动**
   `python main.py`(AI 自启用 `pythonw main.py --headless`)。母本那台用 8712 端口。

## 四、接着聊的正确姿势

新主机上要么把会话文件放回上面位置,要么直接在对话里说:
「读取 <解压路径>/conversations/01-.../d75c9e5f-....jsonl,接续 SPHGJ 项目」。
助手读到 jsonl 就能拿到完整上下文(含每次改动的文件、踩过的坑)。

项目当前状态要点(截至打包时):
- 后端 FastAPI(8712) + 前端 React/TS/Vite;SQLite 库 `data/sphgj.db`
- 归属主键已全改为视频号 `finder_id`(rev23 作品 / rev24 评论)
- 已上线: 自动发作品(rev25/26)、作品播放 + 封面点击 + 标题复制(rev27)、
  账号远程同步浏览(CDP screencast,5 分钟空闲断开)
- 铁律: 改本地 D:\\SPHGJ → 投递 `\\\\Sphgj\\sphgj` → 重启 8712 → 脱敏推 git

## 五、敏感提示

会话记录里出现了: 视频号 finder_id、账号卡片 id、店铺/直播间名称、业务数据、
以及本机路径。属于你自己的生产资料,**不要把云盘这个目录设为公开/分享链接**。
config.json 与 profiles(登录态)没有放进包里。
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    zip_path = os.path.join(OUT_DIR, "SPHGJ-会话记录-%s.zip" % stamp)

    counts = [0, 0, 0]
    files = []          # (abs, arcname)
    for i, (src, arc, desc) in enumerate(SOURCES):
        if not os.path.isdir(src):
            continue
        for fn in sorted(os.listdir(src)):
            p = os.path.join(src, fn)
            if not os.path.isfile(p):
                continue
            if fn.endswith(".jsonl") or fn.endswith(".meta.json"):
                files.append((p, arc + "/" + fn))
                if fn.endswith(".jsonl"):
                    counts[i] += 1

    mem_files = []
    if os.path.isdir(MEM):
        for root, _dirs, fns in os.walk(MEM):
            for fn in fns:
                if fn.endswith(".md"):
                    p = os.path.join(root, fn)
                    rel = os.path.relpath(p, MEM).replace("\\", "/")
                    arc = ("memory/" + rel) if rel.startswith("automations") else ("memory/" + fn)
                    if rel.startswith("automations"):
                        arc = "automations/" + rel.split("/", 1)[1]
                    mem_files.append((p, arc))

    readme = README.format(now=datetime.now().strftime("%Y-%m-%d %H:%M"),
                           n1=counts[0], n2=counts[1], n3=counts[2])
    readme_path = os.path.join(OUT_DIR, "README-恢复指引.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme)

    raw_total = 0
    # 当前会话的 jsonl 仍在被写入,先做快照副本再打包,避免 zip 里记的 size 与实际不符
    # 副本放系统临时目录(不放项目里):平台 safe-delete 钩子会拦项目内批量删除
    stage = tempfile.mkdtemp(prefix="sphgj_pack_")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("README-恢复指引.md", readme.encode("utf-8"))
            for i, (p, arc) in enumerate(files + mem_files):
                sp = os.path.join(stage, "%03d_%s" % (i, os.path.basename(p)))
                shutil.copy2(p, sp)
                z.write(sp, arc)
                raw_total += os.path.getsize(sp)
    finally:
        try:
            shutil.rmtree(stage, ignore_errors=True)
        except Exception:
            pass

    size = os.path.getsize(zip_path)
    print("会话文件: 主工作区 %d 个 / d-SPHGJ %d 个 / fsdownload %d 个" % tuple(counts))
    print("记忆文件: %d 个" % len(mem_files))
    print("原始 %.1f MB -> 压缩 %.1f MB (%.0f%%)" % (raw_total / 1048576.0,
                                                    size / 1048576.0,
                                                    100.0 * size / max(raw_total, 1)))
    print("输出: %s" % zip_path)
    print("README: %s" % readme_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
