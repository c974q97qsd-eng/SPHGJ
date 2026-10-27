# SPHGJ 视频号工具

桌面工具,把多个视频号账号的**直播数据、作品、评论区**集中到一处盯盘:扫码登录、多账号直播大屏、作品批量隐藏/置顶、评论集中查看与自动回复、关键字自动删除、增量抓取、导出。

> 完整功能清单(账号锁定、隐藏发出的评论、评论分页、局域网共享、远程重启等)见 [`docs/功能说明.md`](docs/功能说明.md)。

## 架构
**pywebview 桌面窗口 + FastAPI 后端 + React(TS+Tailwind+shadcn/ui)前端**。视频号 API 需同源 cookie,登录态由后端 Playwright 驱动的 Chromium 持有;前端是控制台,经 REST/WebSocket 调后端。详见 `CLAUDE.md`。

## 数据源
视频号助手「互动管理 -> 评论」(`channels.weixin.qq.com/platform/interaction/comment`),纯 API 抓取:
- `post/post_list` 视频列表(oid + commentCount + 分页)
- `comment/comment_list` 评论列表(分页)
- `comment/create_comment` 回复 / 发评论
- `comment/set_top_comment` 置顶
- `comment/del_comment` 删除
- `comment/update_feed_comment` 标记已读

### 直播数据(dashboardV4,同源 POST)
| 接口 | 内容 |
|---|---|
| `statistic/get_ec_conversion_dashboard_data_v3` | 直播概览与转化:累计观看 / 在线 / 峰值 / 互动 / 粉丝团… |
| `svrkit/MmFinderECAssistantDataSvr/getLiveDistributionChannel` | 成交来源分布(公域 / 加热 / 私域) |
| `statistic/get_live_ec_data_summary` | 整场成交汇总:GMV / 支付 / 客单价 / 退款 / 佣金… |

`check_live_status` / `get_live_info` 负责「是否在播 + 直播流地址(.flv) + 实时在线人数」。
以上统一归一成 **29 个指标**(定义在 `backend/metrics.py`),前端可自选 9 项上卡片。

### 作品管理
基址 `/micro/content/cgi-bin/mmfinderassistant-bin`:
- `post/post_list` 作品列表(分页 + 置顶序)
- `post/update_sticky_status` 置顶 / 取消置顶
- `post/post_update_visible` 隐藏(设为仅自己可见)/ 取消隐藏

## 安装运行

### 三种装法(按场景选)

| 场景 | 做法 |
|---|---|
| **git 克隆后第一次跑(推荐)** | 双击 **`一键安装环境.bat`** — 自动定位 Python(没有就调 winget 装)→ 装 `requirements-lock.txt` → 装 Chromium → 检测 WebView2 / .NET / 前端文件,跑完直接问你要不要启动 |
| **以后升级到最新版** | 双击 **`一键更新.bat`** — 自动拉取最新代码(直连失败自动换国内镜像) → 依赖清单变了才重装 → 提示重启。`data\` / `config.json` / `profiles\` **不会被覆盖** |
| 老流程(setup + start) | 双击 `setup.bat` 初始化(装依赖 + Playwright Chromium + 检测 WebView2),之后双击 `start.bat` 运行 |
| 全新机器 / 无网络 | 用 `配置环境安装\一键安装.bat`(便携 Python 3.14 + 离线 wheels + Chromium 1223,详见该目录 `安装说明.md`;**此目录 1.1G,不入 git,需单独拷**) |
| 已有 Python 环境 | `pip install -r requirements-lock.txt` → `playwright install chromium` → `python main.py` |

> 三个脚本的关系:`一键安装环境.bat` 只解决「第一次跑起来」,可以重复跑(已装好的会跳过);`一键更新.bat` 只解决「升到最新版」,不改本地数据;`启动-SPHGJ.bat` 是日常启动入口,会自己判断用便携版 Python 还是系统 `python`,所以干净克隆的目录也能直接双击启动。

> **连不上 github 怎么办**:`一键更新.bat` 会先直连 GitHub,失败后自动换两个国内镜像通道(`ghfast.top` / `gh-proxy.com`)重试,单个通道最多等 30 秒。三个都不通才会报错——这时看提示:能挂代理就设 `HTTPS_PROXY` 再重跑,完全上不了外网就去云盘下最新 zip 包解压覆盖(只覆盖程序文件,`data\` / `config.json` / `profiles\` 都保留)。

> **提示「本机版本和远端不是同一条历史」**:说明这台机器是很早以前用 zip 包 + 自己 `git init` 装的,历史和仓库对不上。脚本会问要不要对齐,输入 `Y` 就自动 `git reset --hard` 到最新版——只替换程序文件,`data\` / `config.json` / `profiles\` 一律不动,账号不会掉。

> **谁该用一键更新**:只有**从 git 克隆出来**的机器才用。程序实际跑的那台「运行主机」不要用——它的代码是由开发机投递同步的,`.git` 目录并不跟踪实际文件,在那里跑更新会去装 Git、改 `.git/config`,只会把状态搞乱。这类机器在项目根目录放一个空的 `.no_git_update`,`一键更新.bat` 见到它就只打印说明然后退出(该文件已在 `.gitignore` 里,不会传到别人机器上)。

> **依赖清单有两个**:`requirements.txt` 是范围写法(宽松,能用但有版本漂移风险),`requirements-lock.txt` 是与实测环境一致的**精确版本**(推荐)。前端依赖由 `frontend/package.json` + `frontend/package-lock.json` 锁定。

> **Playwright 浏览器版本必须是 chromium revision 1223**(对应 playwright 1.60)。`playwright install chromium` 会自己装对版本;从别的机器直接拷 `ms-playwright` 时拷错 revision 会报 `Executable doesn't exist`。

> **前置组件**:.NET 6/8/9 任一(pywebview 经 pythonnet 调用) + WebView2 Runtime(Win10 21H2+ / Win11 自带)。缺失时桌面窗口打不开,软件会弹原生提示,不会降级用系统浏览器。

> **前端已预构建**(`frontend/dist/` 随仓库),日常不需要 Node.js。仅改前端时用 Node 18+:在 `frontend/` 下 `npm install && npm run build`。

> **仅以桌面窗口运行**:窗口创建失败时弹原生消息框提示安装 WebView2 Runtime,绝不降级为浏览器打开。无图形服务器开发调试可设环境变量 `PINLUN_FORCE_BROWSER=1` 临时回退。

开发模式:前端 `npm run dev`(5173),后端 `python -m uvicorn backend.server:app --port 8000`。

### 换机 / 换主机部署清单

代码走 git(`git clone` + 双击 `一键安装环境.bat` 补依赖),但**下面这些不在 git 里,必须单独拷**:

| 目录 / 文件 | 为什么不在 git | 说明 |
|---|---|---|
| `data\`(含 `sphgj.db`) | 生产数据、体积大 | 整个目录拷;**程序停机后再拷**,或在跑的时候连 `-wal` / `-shm` 一起拷 |
| `config.json` | 含账号卡片与登录态 | 从旧机拷(仓库里只有 `config.example.json`) |
| `profiles\` | 视频号 cookie | 不拷就得重扫每个号的码 |
| `配置环境安装\`(1.1G) | 体积大、可再生 | 离线环境包;无网机器必需 |
| `frontend\node_modules` | 可再生(202M) | 不拷就 `npm install` 重装 |

老库升到当前版本前,先跑 `python tools\upgrade_check.py` 体检(见该脚本说明):它会指出哪些账号卡片缺视频号映射 —— 作品归属是**一次性迁移**,首次启动没映射上就不会再自动纠正。

## 添加账号(扫码登录,软件自动抓取字段)
1. 「账号管理」->「添加账号」-> 弹出二维码。
2. 微信扫码并确认 -> 软件自动抓取 `_aid` / `_log_finder_id` / 名称(拦截 `post_list` 请求 + 读 DOM)。
3. 确认字段 -> 保存。账号 ID 由软件自动生成,cookie 持久化到 `profiles/<id>/`(之后免扫码)。
4. 默认是「网页二维码」:二维码直接推到这个界面里扫,**本机也不弹浏览器窗口**
   (账号管理里定的规矩,保持本机和远程同一套体验)。
   想让本机弹 Chromium 窗口扫,两个办法:
   - 登录弹窗里点「本机窗口」(只对这一次生效)
   - `config.json` 里加 `"login_popup_window": true`(之后一直弹)

> 选择器可能随微信改版漂移,在 `backend/selectors.py` 调整;首跑若二维码截不到用「在新窗口扫码」兜底。

## 直播大屏(多账号同屏盯盘)

一个页面同时盯住**所有账号**的直播:每台账号一张竖屏直播画面 + 一张实时数据卡片。

- **实时画面**:直接播各账号的直播流(FLV),竖屏 342×608 排成一行,谁在播一眼就看到。
  默认只显示**直播中**的账号,一个开关可切换成全部账号。
- **画面下方的三个数**:当前在线 / 公域进房(自然流量) / 公域成交占比(自然 GMV),
  半透明压在画面上,不用切后台。
- **9 项卡片指标可自定义**:每张卡片下方 3×3 九个指标,点「卡片指标设置」自己挑
  (必须正好 9 个、不可重复)。指标库覆盖 7 类共 **29 项**:
  - **观看与互动** — 累计观看人次 / 次数、当前在线、峰值在线、分钟均在线、人均观看时长、
    新增关注、新增粉丝团、评论人数 / 次数、分享人数 / 次数、点赞人数 / 次数、
    曝光人数 / 次数、商品观看次数
  - **转化** — 进房人次 / 次数、曝光人数 / 次数
  - **流量来源** — 公域进房、加热进房
  - **人群画像** — 男性占比、女性占比
  - **成交分布** — 公域成交占比 / 金额、加热成交金额、加热千次成交
  - **成交汇总** — 总成交 GMV、支付人数 / 笔数、点击人数 / 次数、客单价、观众支付率、
    点击支付率、新买家、退款率、退款金额、预估佣金、累计观众
  - **本地增值** — 10 分钟观看增值、10 分钟 GMV 增值、30 分钟 GMV 增值、直播时长
- **数据实时推送**:指标经 WebSocket 推到前端,不用手动刷新。
- **一键跳官方大屏**:卡片上点外链图标直接打开官方 dashboardV4,关掉后自动恢复。

## 作品管理

所有账号发布的作品(封面、标题、播放、点赞、评论、转发、收藏等)自动同步到本地库,
浏览和排序**不再请求官方接口**,打开即毫秒级。

- **封面本地化**:封面只从官方下载一次,之后前端走本地 `/media/covers`,不重复拉 CDN。
- **筛选与排序**:按账号、发布日期范围筛选;按发布时间 / 播放 / 点赞 / 评论 / 转发 /
  收藏排序,快速找出该隐藏的旧作品、该加推的爆款。
- **批量操作**:勾选多个作品,批量「隐藏(设为仅自己可见)/ 取消隐藏 / 置顶 / 取消置顶」;
  也支持按发布日期范围筛完一键批量隐藏。官方对置顶有数量上限(最多 3 个),超出会被拒绝。
- **首次全量、之后增量**:第一次刷新把该账号**全部作品**逐页抓完并记住「已全量」,
  之后每轮只抓有新变化的头部几页 —— 作品再多也不会只显示前 200 条;数据没变化就不重复抓。
- **每日定时刷新**:在线账号每天自动同步一次,也可手动点刷新。
- **进度可见**:顶栏实时显示「今日已请求官方接口 N 次」;刷新任务带进度查询。

## 自动评论(新视频自动发 + 置顶)
在「账号管理」每张账号卡片内配置:Switch 启用 + 评论内容。检测到新视频发布(post_list 返回本地无记录的视频)后自动发评论 + 置顶,同一视频不重发。运行中可随时改,热更新。

## 关键字自动回复
「自动回复设置」页,添加规则 `关键字 -> 回复内容`,启用开关。抓到新评论命中关键字自动回复(同条只回一次)。对所有账号生效。

## 手动回复 / 删除
「评论」页点击评论行 -> 右侧抽屉:回复(Enter 发送)/ 删除 / 置顶。仅选中评论时出现回复 UI。

## 抓取优化
- `post_list` 返回 commentCount,**=0 跳过**。
- 本地存每视频上次 commentCount,**没增加跳过**。
- 评论按 commentId 去重。
- 新评论经 WebSocket 实时推到前端列表顶部。

## 打包 exe(内置 Chromium)
Windows 下双击 `build.bat`(需先 `pip install pyinstaller` + `playwright install chromium` + Node.js)。
产物:`dist/wx-comment-manager/wx-comment-manager.exe` + `browsers/`(Chromium)。

## 文件结构
见 `CLAUDE.md`。UI 规范见 `docs/ui-spec.md`。

## 说明
- 多账号并行:每账号独立 browser(持久化 profile),资源随账号数增加。
- 删除评论 API 已接入(`del_comment`)。
