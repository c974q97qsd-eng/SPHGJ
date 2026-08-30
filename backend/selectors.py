"""视频号助手登录页/平台页 DOM 选择器(集中可调)。

二维码与账号名选择器可能随微信前端改版漂移;首跑若失效在此调整,
或直接用「在新窗口扫码」兜底。命名候选按优先级顺序尝试。
"""
# 视频号助手登录页二维码(尝试多个候选,截图元素)
QR_CANDIDATES = [
    "img.qrcode",
    "img[class*='qrcode']",
    "img[class*='qr-code']",
    ".login__type__container__qrcode img",
    ".qrcode-area img",
    ".weui-desktop-login__qrcode img",
    "canvas[class*='qrcode']",
    ".login_qrcode img",
]

# 登录后平台顶栏账号名(用于自动抓取「名称」)
ACCOUNT_NAME_CANDIDATES = [
    ".account-info .name",
    ".account-info",
    ".weui-desktop-account__name",
    ".account-name",
    "[class*='account'] [class*='name']",
    ".weui-desktop-nav__account-name",
    ".header-account-name",
]

# 扫码后「选择视频号登录」页标记词。
# 该页 URL 仍含 /login(实测),因此只能按 DOM 文本判定;标记词不得与二维码登录页
# 文案(如「登录视频号助手」「扫码登录」)重合,否则会把二维码页误判成选择页。
ACCOUNT_SELECT_MARKERS = (
    "选择视频号登录",
    "请选择要登录的视频号",
    "选择要登录的视频号",
    "选择登录的视频号",
    "使用其他账号登录",
)
# 兜底:仍在 /login 且角色词出现 >=2 次(选择页每个账号卡片都带角色)
ACCOUNT_SELECT_ROLE_WORDS = ("超级管理员", "管理员", "运营者", "创作者")

LOGIN_URL = "https://channels.weixin.qq.com/platform/login"
COMMENT_URL = "https://channels.weixin.qq.com/platform/interaction/comment"
POST_CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"  # 「打开」按钮 goto 此页(发布作品)
LIVE_URL = "https://channels.weixin.qq.com/platform/live/liveBuild"
LIVE_INFO_SELECTOR = ".live-info-container"
LIVE_PLAYER_SELECTOR = ".live-player-video-element"
