# pinlun 项目说明(全局配置)

SPHGJ 视频号工具:多账号视频评论区的集中查看、扫码登录、自动评论、关键字自动回复、手动回复、增量抓取、导出。

> 本文件是本项目的全局配置,Claude Code 每次会话自动加载。改动 UI 前必读下文「UI 开发规范」并遵守。

## 架构

桌面应用 = **pywebview 窗口(主线程) + FastAPI 后端(后台线程,uvicorn 事件循环) + React 前端**。
视频号 API 需同源 cookie,故登录态由**后端 Playwright 驱动的 Chromium** 持有;React 前端只是控制台,通过 REST/WebSocket 调后端。

```
pinlun/
├── main.py                  入口:起 FastAPI 线程 + 开 pywebview 窗口(Windows 强制 WebView2 原生窗口,不回退浏览器)
├── backend/
│   ├── server.py            FastAPI:REST + WS,托管 frontend/dist,事件广播 Hub
│   ├── schemas.py           pydantic 请求/响应模型
│   ├── account_manager.py   多账号 worker + 登录会话 + 自动评论热更新 + 状态上报
│   ├── login_capture.py     ★ 扫码登录:QR 截图推送 + 请求拦截抓 _aid/_log_finder_id/名称 + 兜底弹窗
│   ├── selectors.py         登录页/平台页 DOM 选择器(集中可调)
│   ├── api_client.py        视频号 API(post_list/comment_list/create_comment/...)
│   ├── comment_fetcher.py   增量抓取引擎(返回新评论列表供 WS 推送)
│   ├── auto_reply.py        关键字自动回复
│   ├── auto_comment.py      新视频自动评论 + 置顶
│   └── storage.py           SQLite(评论 + 视频评论数 + 自动评论记录)
├── frontend/                React + Vite + TS + Tailwind + shadcn/ui
│   └── src/{components/{ui,layout,accounts,comments,common}, pages, hooks, lib, styles}
├── config.json              账号 / 间隔 / 自动回复配置(运行时读写)
├── data/                    SQLite 数据库
├── profiles/                每账号 Playwright profile(cookie 持久化)
├── docs/ui-spec.md          UI 规范详文(设计 token / 组件 / 评审清单)
├── legacy/gui_app.py        旧 Tkinter UI(已废弃,保留备用)
└── start.bat / setup.bat / build.bat
```

### 关键流程
- **添加账号**:`POST /api/accounts/login/start` -> 后端起 headless Playwright,goto 评论页(未登录跳登录页)-> 截图二维码经 WS 推前端 -> 用户扫码 -> URL 回评论页 -> 拦截首个 `post_list` 请求抓 `_aid`(URL)/`_log_finder_id`(body)+ DOM 抓名称 -> 前端确认 -> `finalize` 落盘 profile 改名 + 写 config。兜底:`open-window` 用同 profile 重启 headed 让用户在弹出窗口扫码。
- **自动评论**:配置在账号管理模块内,每账号 Switch + 内容,`PATCH /api/accounts/{id}` 热更新到运行中 worker。
- **手动回复**:仅选中评论时触发右侧 Sheet 抽屉(回复/删除/置顶),无常驻回复栏。
- **实时**:后端 Hub 把 `qr_update`/`login_status`/`engine_status`/`comments_update` 广播给所有 WS 客户端。

## 运行 / 打包
- 开发:`cd frontend && npm run dev`(5173,代理 /api /ws 到 8000);另起 `python -m uvicorn backend.server:app --port 8000`。
- 桌面:`python main.py`(起服务 + pywebview 窗口)。
- 初始化:`setup.bat`(装 Python 依赖 + Playwright Chromium + 前端构建)。
- 打包:`build.bat`(PyInstaller + 前端 dist + 内置 Chromium)。

---

## UI 开发规范(全局硬约束)

**技术栈:React + TypeScript + Tailwind CSS + shadcn/ui + Radix UI + Lucide**

1. **遵循 8px 栅格系统**,全局引用 CSS 变量主题(`src/styles/globals.css` 的 `--background/--foreground/--primary/...`),保持全页面视觉一致,**禁止随机样式**。Tailwind 间距只用偶数档(p-2=8px / p-4=16px / p-6=24px / p-8=32px)。
2. **符合 WCAG 无障碍标准**:添加 ARIA 标签、键盘导航、焦点状态(`focus-visible:ring`),支持深色/浅色主题(`ThemeProvider` class 策略)、移动端优先响应式布局(侧栏窄屏折叠)。
3. **建立清晰信息层级**:核心操作突出(主色按钮),次要信息弱化(`text-muted-foreground`);增加留白,避免堆砌元素。
4. **适度微交互**:hover/active/loading 状态,柔和动画(`tailwindcss-animate` / Framer Motion),**禁用过度花哨特效**。
5. **完善异常状态**:空数据(`EmptyState`)、加载(`LoadingState`/`Skeleton`)、错误(`ErrorState`+重试)、表单校验提示(toast),文案简洁人性化。
6. **拒绝通用 AI 模板 UI、泛滥渐变、同质化布局**;保持品牌视觉风格(紫色主色系,延续 `#7c83ff` 观感)。
7. **代码模块化拆分组件**,可复用可维护,添加注释。UI 原子在 `components/ui/`,业务组件按域分目录(`accounts/`、`comments/`),通用态在 `common/`。
8. **完成后做 UX 评审**:检查易用性、信息可读性、操作流程合理性(评审清单见 `docs/ui-spec.md`)。

### 约定
- 新增 UI 组件优先复用 `components/ui/` 已有 shadcn 原语;缺则补,勿重复造。
- 颜色/圆角/间距一律走 CSS 变量与 Tailwind 主题,**不硬编码色值**(品牌紫已在 `--primary`)。
- 图标统一用 `lucide-react`,尺寸 `h-4 w-4`/`h-5 w-5`。
- 所有交互元素带 `aria-label`(纯图标按钮必备)。
- 数据获取走 `hooks/`,REST 走 `lib/api.ts`,WS 走 `lib/ws.ts`(`useWebSocket`),勿散落 fetch。
- 后端字段命名 snake_case,前端 interface 可保持 snake_case 以对齐 API(避免无谓转换)。
