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
    ".weui-desktop-account__name",
    ".account-name",
    "[class*='account'] [class*='name']",
    ".weui-desktop-nav__account-name",
    ".header-account-name",
]

LOGIN_URL = "https://channels.weixin.qq.com/platform/login"
COMMENT_URL = "https://channels.weixin.qq.com/platform/interaction/comment"
LIVE_URL = "https://channels.weixin.qq.com/platform/live/liveBuild"
LIVE_INFO_SELECTOR = ".live-info-container"
LIVE_PLAYER_SELECTOR = ".live-player-video-element"
