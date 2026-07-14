# UI 开发规范详文

技术栈:**React + TypeScript + Tailwind CSS + shadcn/ui + Radix UI + Lucide**。

## 设计 token

### 色彩(CSS 变量,HSL,见 `frontend/src/styles/globals.css`)
| token | 浅色 | 深色(默认) | 用途 |
|---|---|---|---|
| `--background` | 0 0% 100% | 240 23% 13% | 页面底 |
| `--card` | 0 0% 100% | 240 20% 17% | 卡片/面板 |
| `--primary` | 238 84% 60% | 238 90% 74% | 品牌主色(紫) |
| `--muted-foreground` | 240 4% 46% | 240 8% 65% | 次要文字 |
| `--success` | 142 60% 45% | 142 55% 55% | 在线/成功 |
| `--warning` | 38 92% 50% | 38 92% 58% | 未登录/警示 |
| `--destructive` | 0 72% 51% | 0 70% 60% | 删除/危险 |
| `--border` | 240 6% 88% | 240 16% 28% | 分隔线/边框 |

品牌主色为紫色系,延续原 `#7c83ff` 观感;深色为默认主题。

### 间距(8px 栅格)
Tailwind 默认 4px 基数,约定**只用偶数档**:`gap-2`(8)/ `p-4`(16)/ `p-6`(24)/ `p-8`(32)。组件内紧凑区可用 `p-3`(12)/ `gap-1.5`(6)。

### 圆角 / 字号 / 阴影
- 圆角:`--radius: 0.625rem`,组件用 `rounded-md`/`rounded-lg`。
- 字号:`text-sm`(14)正文、`text-base`(16)标题、`text-xs`(12)辅助、`text-lg`/`2xl` 数据。
- 阴影:`shadow-sm`(卡片)/ `shadow-lg`(弹窗),禁重投影。

## 组件清单(`frontend/src/components/`)
- **ui/**(shadcn 原语):button、card、input、textarea、label、badge、switch、dialog、sheet、select、table、tabs、scroll-area、skeleton、separator、tooltip、sonner。
- **layout/AppShell**:侧栏导航 + 顶栏(引擎启停/立即抓取/间隔/主题切换)+ 主区。
- **common/**:`EmptyState`、`LoadingState`、`ErrorState`、`StatusBadge`。
- **accounts/**:`AddAccountDialog`(二维码扫码 + 字段确认)。
- **comments/**:评论表 + 上下文回复 Sheet。

## 页面
1. **仪表盘** `/`:概览卡(账号/评论/已回/新增)+ 账号状态 + 最近评论。
2. **账号管理** `/accounts`:账号卡片(状态/统计/启停/删除)+ **每账号自动评论配置**(Switch + Textarea,热更新)+ 添加账号(扫码)。
3. **评论** `/comments`:筛选(账号/搜索/已回)+ 评论表;**选中行 -> 右侧 Sheet 手动回复**(回复/删除/置顶),未选中无回复 UI。
4. **自动回复设置** `/auto-reply`:全局开关 + 关键字规则编辑器(关键字 -> 回复)。

## 交互/动效
- hover/active:`hover:bg-accent`、`active:scale-[0.98]`(按钮)。
- 进场:`animate-slide-up`(卡片)、`data-[state=open]:animate-in`(弹窗/抽屉)。
- 新评论:WS 推送前置插入,可加 `bg-primary/5` 短暂高亮。
- 禁花哨:无大范围渐变、无粒子、无弹跳。

## 无障碍(WCAG)
- 纯图标按钮必带 `aria-label`;表单 `Label htmlFor` 关联。
- 键盘:Enter 提交回复(Shift+Enter 换行)、Esc 关弹窗、Tab 顺序合理。
- `focus-visible:ring-2 ring-ring ring-offset-2`;色对比满足 AA。
- 主题切换持久化(localStorage `pinlun-theme`),`<html>` class 切换。

## 异常态
- **空**:无账号 -> 引导添加;无评论 -> 说明启动引擎后自动抓取。
- **加载**:`Skeleton`(列表)/ `Loader2 animate-spin`(按钮)。
- **错误**:`ErrorState` + 重试;接口失败 `toast.error` 带原因。
- **校验**:启用自动评论但内容空 -> 阻止保存并提示;间隔 <10s -> 报错。

## UX 评审清单(完成后逐项过)
- [ ] 信息层级:核心操作一眼可见,次要信息弱化。
- [ ] 留白充足,无元素堆砌。
- [ ] 全链路有空/加载/错误态。
- [ ] 表单校验提示明确、人性化。
- [ ] 键盘可完成主流程(添加账号除外,需扫码)。
- [ ] 深/浅色主题下均可读、对比达标。
- [ ] 窄屏(移动)布局不破,侧栏折叠。
- [ ] 微交互克制,无干扰动画。
- [ ] 品牌一致(紫色主色),无 AI 模板感。
- [ ] 文案简洁中文,无占位英文残留。
