# -*- coding: utf-8 -*-
"""rev25-4b: 前端挂路由 + 侧栏菜单「发布管理」。"""
import io, sys

# ---------- App.tsx ----------
PA = r"D:\SPHGJ\frontend\src\App.tsx"
s = io.open(PA, encoding="utf-8").read()
assert "PublishPage" not in s
OLD = 'import { PostsPage } from "@/pages/PostsPage"'
assert s.count(OLD) == 1
s = s.replace(OLD, OLD + '\nimport { PublishPage } from "@/pages/PublishPage"', 1)
OLD2 = '                <Route path="/posts" element={<PostsPage />} />'
assert s.count(OLD2) == 1
s = s.replace(OLD2, OLD2 + '\n                <Route path="/publish" element={<PublishPage />} />', 1)
io.open(PA, "w", encoding="utf-8", newline="").write(s)

# ---------- AppShell.tsx ----------
PS = r"D:\SPHGJ\frontend\src\components\layout\AppShell.tsx"
s = io.open(PS, encoding="utf-8").read()
OLD3 = "  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard,"
assert s.count(OLD3) == 1
s = s.replace(OLD3, "  Play, Square, RefreshCw, Loader2, Monitor, Type, Clapperboard, Send,", 1)
OLD4 = '  { to: "/posts", label: "作品管理", icon: Clapperboard },'
assert s.count(OLD4) == 1
s = s.replace(OLD4, OLD4 + '\n  { to: "/publish", label: "发布管理", icon: Send },', 1)
OLD5 = '"/live-screen": "直播大屏", "/posts": "作品管理" }'
assert s.count(OLD5) == 1
s = s.replace(OLD5, '"/live-screen": "直播大屏", "/posts": "作品管理", "/publish": "发布管理" }', 1)
io.open(PS, "w", encoding="utf-8", newline="").write(s)

chk = io.open(PA, encoding="utf-8").read() + io.open(PS, encoding="utf-8").read()
must = ['path="/publish"', "PublishPage", '"发布管理"', "icon: Send"]
miss = [m for m in must if m not in chk]
print("SELFCHECK", "PASS" if not miss else f"FAIL {miss}")
sys.exit(1 if miss else 0)
