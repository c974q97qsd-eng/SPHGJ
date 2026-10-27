# 抖音创作者中心接口实测清单（2026-10-28）

> 全部在**真实登录会话**（测试号，身份信息已脱敏）里实测得到，
> 不是从文档抄的。读接口走页面内 `fetch` 抓包，写接口全部**真实执行过一次**并验证回滚。
>
> 探测工具在本目录（`dyprobe/`），用法见文末。

---

## 0. 基础约束（踩过的坑，先看这段）

### 域名
必须用 **`creator.douyin.com`**，不是 `www.douyin.com`。
后者是公开内容侧，签名方式完全不同（需 `X-Bogus`），管不了自己的作品。

### 鉴权
| 项 | 说明 |
|---|---|
| cookie | `.douyin.com` 域下的会话 cookie |
| `x-secsdk-csrf-token` | **写操作必需**。`000100000001<hex>…`，由 `POST mssdk.bytedance.com/web/common` 换取，与页面会话绑定 |
| `msToken` | 部分接口 query 里带，缺失时多数接口仍可用 |
| `aid=2906` / `read_aid=2906` | 固定值（创作者中心） |

> 结论：**写操作必须先进浏览器同源 fetch**。抖音不像视频号那样能直接纯 httpx 跑写接口。

### 🚨 错误语义（最容易踩的坑）
未知路径返回：

```json
{"status_code": 0, "status_msg": "url doesn't match"}
```

**`status_code` 仍然是 0！** 不能用 `status_code == 0` 判断成功。

**可靠的命中信号：`extra` 字段。**
| 返回 | 含义 |
|---|---|
| `{"extra":{"now":…},"status_code":0,"status_msg":""}` | ✅ 真接口，成功 |
| `{"extra":{"logid":"…"},"status_code":0}` | ✅ 真接口，成功 |
| `{"status_code":0,"status_msg":"url doesn't match"}` | ❌ 路径不对 |
| `{"status_code":18,"status_msg":"No item permission"}` | ⚠️ 路径对，但参数名/格式错（删作品用了 aweme_id） |
| `{"status_code":4,"status_msg":"啊哦，服务器打瞌睡了"}` | ⚠️ **服务端按对象拒绝**，不是参数错（见 §1.2 注） |
| `{"status_code":5,"status_msg":"参数不合法"}` | ⚠️ 缺必需参数（如 reply/list 缺 item_id） |

→ 判定函数只认 `"extra" in resp`。

### 编码差异（同一个站里两种混用）
| 接口类 | 编码 |
|---|---|
| 评论相关（reply/delete） | **JSON** |
| 作品相关（item/top、aweme/delete） | **`application/x-www-form-urlencoded`** |
| 发布 `create_v2` | **JSON** |

---

## 1. 评论管理

### 1.1 评论列表
```
GET /aweme/v1/creator/comment/list
    ?cursor=0&count=20&item_id=<URL编码的加密 item_id>&sort=TIME&aid=2906
```
- `sort`: `TIME` / `HOT`
- **`item_id` 必须是加密串**（`@jPdq67…==`），明文 aweme_id 无效
- 分页：`cursor` 从 0 递增（不是时间戳）
- ⚠️ `total_count` 只算**一级评论**，不含子回复；而作品列表的
  `statistics.comment_count` **含子回复**。实测同一作品：统计 2 / total_count 1。
  → 两者只能用来判断「有没有变化」，**绝不能相等比较**，否则会一直重复抓。
- 响应关键字段：

```json
{"comment_info_list":[{
  "comment_id":"@jPdq…==", "text":"…", "create_time":"1788187732",
  "digg_count":"0", "reply_count":"1", "status":1,
  "level":1, "is_author":false,
  "user_info":{"screen_name":"桔梗^","user_id":"@jPdq…", "avatar_url":"…"}
}], "cursor":1, "has_more":false, "total_count":1}
```

### 1.2 子回复列表
```
GET /aweme/v1/creator/comment/reply/list
    ?comment_id=<URL编码>&item_id=<URL编码>&cursor=0&count=20&aid=2906
```
返回结构与评论列表一致；子回复 `level: 2`。

> ⚠️ **个别作品会被服务端拒绝**：返回 `status_code=4 "啊哦，服务器打瞌睡了"`。
> 实测（2026-10-28）换 `count` / `cursor` / 去掉 `item_id` 全都一样，
> 换另一条作品立刻成功（level=2 正常返回）→ 是**按作品拒绝**，不是我们拼错参数。
> 少 `item_id` 才会变成 `status_code=5 "参数不合法"`，可据此区分两类错误。
> 工程结论：`reply/list` 失败必须**降级跳过**，不能因为一条作品的子回复拿不到就判整轮失败。

### 1.3 回复评论 ✅ 已真实执行
```
POST /aweme/v1/creator/comment/reply/?aid=2906&msToken=…
Content-Type: application/json

{"comment_Id":"<加密 comment_id>","item_id":"<加密 item_id>","text":"回复内容"}
```
> ⚠️ `comment_Id` 是**大写 I**，不是 `comment_id`。写错不会报错，会静默失败。

### 1.4 删除评论 ✅ 已真实执行并验证
```
POST /aweme/v1/creator/comment/delete/
Content-Type: application/json

{"comment_id":"<加密 comment_id>","item_id":"<加密 item_id>"}
```
> 注意这里参数是**小写** `comment_id`，跟 reply 的大写 `comment_Id` 不一致。

### 1.5 评论置顶 ❌ 未定型
试过 `/comment/top/`、`/stick/`、`/set_top/`、`/manage/top/`、`/istop/` 全部
`url doesn't match`。评论页 DOM 里只有「回复 / 删除 / 举报」，**没有置顶入口**，
可能创作者中心本身不提供。待确认是否放弃。

---

## 2. 作品管理

### 2.1 作品列表（主用）
```
GET /janus/douyin/creator/pc/work_list
    ?scene=star_atlas&device_platform=android&status=0&count=12&max_cursor=0
    &cookie_enabled=true&screen_width=3840&screen_height=2160&browser_language=zh-CN
    &browser_platform=Win32&browser_name=Mozilla&browser_version=5.0
    &os_name=Windows&os_version=10&platform=PC&channel=channel_pc_web&aid=2906
```
- 🚨 **`max_cursor` 是毫秒时间戳**，不是 offset。首页传 `0`，下一页取响应里的 `max_cursor`
- `count` 实测 12 可用
- `status`: 0 = 全部
- 响应关键字段：

```json
{"aweme_list":[{
  "aweme_id":"7680204627916726001",     // 明文，写操作都用这个
  "desc":"作品文案",
  "status_value":102,                    // 见下表
  "status":{"allow_comment":true,"allow_share":true},
  "Cover":{"uri":"tos-cn-…","url_list":["…"]},
  "create_time":…, "duration":27034
}], "has_more":true, "max_cursor":1787918865000}
```

**`status_value` 语义（实测对照）**
| 值 | 含义 |
|---|---|
| `102` | ✅ 公开正常 |
| `140` | 非公开（实测：一条"轮毂盖可定制"作品） |
| `141` | 非公开（实测：我发的「仅自己可见」测试作品） |

> 判断「这条作品是否仅自己可见」用 `status_value != 102`。

### 2.2 作品列表（带加密 ID，评论要用）
```
GET /aweme/v1/creator/item/list?cursor=&aid=2906
```
- 返回 `item_id`（**加密**，评论接口用）+ `item_id_plain`（数字 aweme_id）
- 🚨 **翻页游标叫 `cursor`，没有 `max_cursor` 字段**（2026-10-28 实测修正）
  - 首页传 `cursor=`（空串），响应里 `cursor` 是**时间戳字符串**，原样回传即可翻页
  - 曾经按 work_list 的习惯读 `max_cursor` → 永远读到 0 → 第一页就停 → 只覆盖 19 条
  - 实测同一账号：`cursor` 翻 5 页覆盖 **90/91** 条作品（唯一缺的是 `status_value=141`
    的私密作品，本来就没有公开评论，属正常排除）
- ⚠️ **每页上限 20 条**：`count` 传 10/20/50/100 分别回 10/19/19/19 条，
  放大 `count` 无效，只能靠翻页（首页常是 19 条，因为有一条被过滤掉）
- ✅ 顺序是**发布时间倒序**（新作品在第 1 页）。原文档写「新作品不一定在第一页」是错的，
  已用首页 19 条 id 与 work_list 时间序逐条对照确认
- ✅ 映射可靠性已核对：前 6 条作品的 `item_id_plain` 对应的 `comment_count`
  与 work_list 的 `statistics.comment_count` **完全一致** → `item_id_plain` 就是明文 aweme_id
- ⚠️ 不支持按 id 过滤：`item_id_plain=` / `item_id=` 参数会被忽略（返回条数不变）
- 返回字段：`item_id` / `item_id_plain` / `comment_count` / `create_time` / `duration` /
  `title` / `cover_image_url` / `item_link` / `media_type` / `creator_item_setting`

### 2.3 作品置顶 ✅ 已真实执行（来回各一次）
```
POST /web/api/media/item/top/?cookie_enabled=true&screen_width=…&aid=2906
Content-Type: application/x-www-form-urlencoded

aweme_id=7680204627916726001&need_top=1
```
- `need_top=1` 置顶 / `need_top=0` 取消
- **表单编码**，参数是**明文** aweme_id

### 2.4 删除作品 ✅ 已真实执行并验证
```
POST /web/api/media/aweme/delete/?cookie_enabled=true&…
Content-Type: application/x-www-form-urlencoded

item_id=7701470168337239296
```
> 🚨 参数名是 `item_id`（值是**明文** aweme_id），**不是 `aweme_id`**。
> 用 JSON body + `aweme_id` 会返回 `status_code:18 "No item permission"`，
> 很容易误判成「没权限」。

### 2.5 作品可见性修改 ❌ 未定型
作品卡片上的 `公开 / 好友可见 / 仅自己可见` 是**内联 checkbox**，需先点「设置权限」才显示。
用 JS `el.click()` 能改勾选状态，但**一个业务请求都没发**（只抓到埋点），
说明提交由其他交互触发或需要额外确认。待补。
另：同一排还有第二组 `允许 / 不允许`（= 是否允许评论）。

### 2.6 编辑作品
路径已从 bundle 扫到：`POST /web/api/media/aweme/update_v2`（未实际执行）。

---

## 3. 发布作品 ✅ 全链路已真实跑通（发布成功，作品数 91→92，随后删除）

### 3.1 完整步骤

```
① GET  /web/api/media/upload/auth/v5/            → 拿上传凭证（含 SessionKey）
② POST https://tos-d-x-hl.douyin.com/upload/v1/tos-cn-v-0015c001/<hash>
        原始二进制 body（Content-Type: video/mp4）
        ← 视频本体**单次直传**（实测 1.47MB 一次传完，大文件才分片）
③ POST https://vod.bytedanceapi.com/
        {"SessionKey":"<base64>"}                ← 提交转码
④ GET  /aweme/v1/cover/gen/ref/                  封面参考（帧候选）
⑤ POST /aweme/v1/cover/gen/post/                 生成封面
        {"cover_uri":"tos-cn-i-jm8ajry58r/…","title":"","caption":"",
         "img_uris":["tos-cn-i-jm8ajry58r/…", …]}
⑥ GET  /aweme/v1/cover/gen/get/?task_id=…        轮询封面结果
⑦ POST /aweme/v1/post_assistant/fast_detect/pre_check   内容预检
   POST /aweme/v1/post_assistant/fast_detect/poll       预检轮询（body 见下）
⑧ POST /web/api/media/aweme/create_v2/?read_aid=2906&…  ← **真正提交发布**
```

`fast_detect` 请求体：
```json
{"resource_list":[{"type":2,"video_id":"v0d00fg…","duration":7.3,"cover_uri":""}],
 "source":1,"is_redetect":false,"task_id":"1466323125312294"}
```

### 3.2 `create_v2` 完整结构（可直接复用）

```json
{"item": {
  "common": {
    "text": "作品文案",              // 正文（含话题时话题也写在这里）
    "caption": "作品文案",           // 与 text 相同
    "item_title": "",
    "activity": "[]",               // 字符串化的 JSON
    "text_extra": "[]",             // 话题/mention 的偏移信息
    "challenges": "[]",             // 话题列表
    "mentions": "[]",               // @好友
    "hashtag_source": "",
    "hot_sentence": "",
    "interaction_stickers": "[]",
    "visibility_type": 1,           // 1 = 仅自己可见（本次实测）
    "download": 1,                  // 允许下载
    "timing": 0,                    // 0 = 立即发布
    "creation_id": "iji5blrk1793138060899",   // 幂等 id，自己生成
    "media_type": 4,                // 4 = 视频
    "video_id": "v0d00fg…",         // 步骤②③拿到的 vid
    "music_source": 0,
    "music_id": null
  },
  "cover": {
    "cover_text_uri": null, "cover_text": null, "poster_delay": 0,
    "cover_tools_extend_info": "{…推荐封面候选…}",
    "cover_tools_info": {}
  },
  "mix": {},                                          // 合集
  "selected_member": {"is_selected_member_video": false},
  "chapter": {"chapter": "{…章节…}"},
  "anchor": {},                                       // 锚点（团购/小程序等）
  "sync": {"should_sync": false, "sync_to_toutiao": 0},  // 同步到头条
  "open_platform": {},
  "assistant": {"is_preview": 0, "is_post_assistant": 1}
}}
```

Header：`Content-Type: application/json` + `x-secsdk-csrf-token` +
`Referer: https://creator.douyin.com/creator-micro/content/post/video`

### 3.3 实测结果
- 发布后作品数 91 → 92
- 新作品 `aweme_id=7701470168337239296`，`status_value=141`（= 仅自己可见生效）
- 已用 2.4 的删除接口删掉，账号恢复 91 条

---

## 4. 页面/入口速查

| 功能 | URL |
|---|---|
| 作品管理 | `creator.douyin.com/creator-micro/content/manage` |
| 评论管理 | `creator.douyin.com/creator-micro/interactive/comment` |
| 发布页 | `creator.douyin.com/creator-micro/content/upload` |
| 评论（旧入口，会重定向） | `…/data/following/comment` → `…/interactive/comment` |

> 创作者中心是 **iframe 微前端**（`/goofy/douyin_creator_pc/…`）。
> 只附着顶层 frame 读 `performance` 抓不到接口，必须**递归遍历同源 iframe**。

---

## 5. 工具（本目录）

| 文件 | 用途 |
|---|---|
| `cdp.py` | 极简 CDP 客户端（只依赖 `websocket-client`）。含事件收集/`eval_obj`/点击 |
| `probe2.py` | 读某页已调过的接口（新开 tab + 递归 iframe 读 performance） |
| `catch_detail.py` | 抓某接口的完整请求+响应 |
| `probe_bundle.py` / `probe_quoted.py` / `probe_snippets.py` | 从 JS bundle 静态提取接口路径 |
| `probe_dom.py` / `probe_work_menu.py` / `probe_perm.py` | 探 DOM / 操作菜单 / 权限面板 |
| `exec_fetch.py` | **页面内直调任意接口**（支持 JSON/form），最适合验证与定型 |
| `exec_comment_reply2.py` / `exec_comment_delete.py` | 评论回复 / 删除的真实执行 |
| `exec_work_ops.py` / `exec_del_work.py` | 作品置顶 / 删除的真实执行 |
| `exec_publish2.py` | 完整发布（上传→填文案→设私密→发布） |

### 关键踩坑
1. `Runtime.evaluate` 传 `() => {...}` 返回的是**函数本身**，必须写 `(() => {...})()`。
2. `Target.setAutoAttach(flatten)` 在浏览器级会把**所有标签页**都附着进来，污染结果。
   改成「新开单标签页 + 页面内递归 iframe」。
3. 坐标点击 `<span>` 文本对 checkbox **不生效**（React 监听的是 input），要 `el.click()` input。
4. 页面里 `hasUploading` 用「上传中|处理中」判断会**误报**（封面处理阶段也有"处理中"字样），
   应只认 `上传中|转码中|\d+%`。
5. 用完必须 `Target.closeTarget`/`/json/close` 并复核没有残留 about:blank。

---

## 6. 已落地为代码（rev56）

接口规格全部收敛到 `backend/douyin/api.py`（纯函数、可离线单测），
客户端 `backend/douyin/client.py` 只负责「把请求送出去」。三处踩过的坑已在代码里封死：

| 坑 | 代码里的落点 |
|---|---|
| `status_code=0` 也可能失败 | `api.judge()` 只认 `"extra" in resp` |
| 参数名大小写/命名不一致 | `comment_reply_body`（大写 I）/ `comment_delete_body`（小写）/ `work_delete_body`（item_id） |
| 三种编码混用 | `api.FORM_PATHS` + `form=True`；`dumps_form` / `dumps_json` |

另外两个实现细节，都是实测踩出来的：

1. **URL 编码只能做一次**。`item_id` 里含 `@` `/` `+` `=`，先 `quote()` 再交给
   `urlencode()` 会把 `%` 编成 `%25`（双重编码），请求必然失败。统一只让 `urlencode` 编码。
2. **账号身份从作品列表的 `author` 取**（`uid` / `sec_uid` / `unique_id`），
   不另找 `/user/info`。这一枪本来就要打，顺手当登录态探针；没有作品时返回 `{}`，
   归属键退化成 `acct:<账号id>`，**不猜**。

验证脚本：`tools/dyprobe/verify_step1.py`（真机 19 项）。

### 6.1 第 2 步（评论抓取入库 + 回复/删除）已落地

新增三个文件，职责严格分开：

| 文件 | 职责 | 离线单测 |
|---|---|---|
| `backend/douyin/fetcher.py` | 作品列表 → 分区 → 加密 id 映射 → 评论分页 → 子回复 → 入库 | `test_rev56b_dy_fetcher.py` 46 项 |
| `backend/douyin/manager.py` | 一个抖音账号的运行容器（**不接管登录/浏览器**，page 靠注入） | 随 verify_step2 |
| `backend/server.py` 新增 6 条路由 | `/api/dy/{status,attach,detach,fetch}` + 评论回复/删除 | — |

复用视频号那套「用血换来的四条规矩」：commentCount 缺失 ≠ 没有评论；
早停前提是 `has_comments`；请求失败不能当成功；子回复失败必须降级。

第 2 步又踩出两个真 bug（都写了回归单测）：

3. **加密 item_id 覆盖不全**：读错游标字段名（见 §2.2），91 条作品只覆盖 19 条，
   表现为「评论只抓 21%、回复老作品报拿不到加密 id」。修好后 90/91。
4. **子回复入库把加密串当 `export_id` 存**：`export_id` 的语义是**明文 aweme_id**，
   存成 `@jPdq…==` 以后 `reply()`/`delete_comment()` 再拿它查映射必然查不到，
   报错还伪装成「拿不到加密 item_id」（误导性报错，多花了两轮排查）。
   现在发请求用加密串、入库用明文，两者在代码里是**两个不同形参**。

验证脚本：`tools/dyprobe/verify_step2.py`（真机 38 项，`--write` 跑真实
「回复 → 找回 → 删除 → 复查消失」全链路，自清理）。
