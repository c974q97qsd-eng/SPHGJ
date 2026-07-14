# SPHGJ 视频号工具

桌面工具,管理多个视频号账号下**视频的评论区**:扫码登录、自动评论、关键字自动回复、集中查看、手动回复、增量抓取、导出。

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

## 安装运行
```bash
pip install -r requirements.txt
playwright install chromium
python main.py        # 桌面窗口(Windows 用 WebView2 原生窗口)
```
Windows 用户直接双击 `setup.bat`(一次性初始化:装 Python 依赖 + Playwright Chromium + 检测/安装 WebView2 Runtime)再双击 `start.bat` 运行。

> **前端已预构建**(`frontend/dist/` 随仓库),`setup.bat` 不再需要 Node.js/npm。仅当修改前端时才需 Node.js 18+:在 `frontend/` 下执行 `npm install && npm run build` 重新构建。

> **仅以桌面窗口运行,不回退系统浏览器**:窗口创建失败时弹原生消息框提示安装 WebView2 Runtime,绝不降级为浏览器打开。无图形服务器开发调试可设环境变量 `PINLUN_FORCE_BROWSER=1` 临时回退。

开发模式:前端 `npm run dev`(5173),后端 `python -m uvicorn backend.server:app --port 8000`。

## 添加账号(扫码登录,软件自动抓取字段)
1. 「账号管理」->「添加账号」-> 弹出二维码。
2. 微信扫码并确认 -> 软件自动抓取 `_aid` / `_log_finder_id` / 名称(拦截 `post_list` 请求 + 读 DOM)。
3. 确认字段 -> 保存。账号 ID 由软件自动生成,cookie 持久化到 `profiles/<id>/`(之后免扫码)。
4. 二维码不显示?点「在新窗口扫码」-> 弹出带界面 Chromium 扫码。

> 选择器可能随微信改版漂移,在 `backend/selectors.py` 调整;首跑若二维码截不到用「在新窗口扫码」兜底。

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
