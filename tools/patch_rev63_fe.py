# -*- coding: utf-8 -*-
"""rev63 前端:api.ts 增 publishResetStreak;SettingsPanel 熔断徽章旁加「解除」按钮。"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "frontend", "src", "lib", "api.ts")
SP = os.path.join(ROOT, "frontend", "src", "components", "publish", "SettingsPanel.tsx")


def apply(path, patches):
    s = io.open(path, encoding="utf-8").read()
    for old, new, tag in patches:
        n = s.count(old)
        assert n == 1, "[%s] 命中 %d 次(应为 1)" % (tag, n)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)


API_PATCHES = [
    (
        '  publishFinders: () => req<{ items: PublishFinder[]; generic_titles: { total: number; unused: number }; mappings: Record<string, string> }>("/publish/finders"),\n',
        '  publishFinders: () => req<{ items: PublishFinder[]; generic_titles: { total: number; unused: number }; mappings: Record<string, string> }>("/publish/finders"),\n'
        '  // rev61: 手动解除某视频号的发布熔断\n'
        '  publishResetStreak: (finderId: string) =>\n'
        '    req<{ ok: boolean }>(`/publish/finders/${encodeURIComponent(finderId)}/reset-streak`,\n'
        '      { method: "POST" }),\n',
        "api publishResetStreak",
    ),
]

SP_PATCHES = [
    (
        "  return (\n    <div className=\"grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]\">\n",
        "  const resetStreak = async (finderId: string) => {\n"
        "    try {\n"
        "      await api.publishResetStreak(finderId)\n"
        '      toast.success("已解除熔断,该号会重新排入队列")\n'
        "      await load()\n"
        "    } catch (e) {\n"
        '      toast.error(`解除失败: ${(e as Error).message}`)\n'
        "    }\n"
        "  }\n\n"
        "  return (\n    <div className=\"grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]\">\n",
        "sp resetStreak",
    ),
    (
        '                  {f.fail_streak >= 2 && <Badge variant="destructive" className="text-[10px] font-normal">已熔断</Badge>}\n',
        "                  {f.fail_streak >= 2 && (\n"
        "                    <>\n"
        '                      <Badge variant="destructive" className="text-[10px] font-normal">已熔断</Badge>\n'
        '                      <Button variant="outline" className="h-5 px-1.5 text-[10px]"\n'
        "                        onClick={() => resetStreak(f.finder_id)}>解除</Button>\n"
        "                    </>\n"
        "                  )}\n",
        "sp badge",
    ),
]


def main():
    apply(API, API_PATCHES)
    apply(SP, SP_PATCHES)
    a = io.open(API, encoding="utf-8").read()
    s = io.open(SP, encoding="utf-8").read()
    checks = [
        ("api 有 publishResetStreak", "publishResetStreak:" in a),
        ("api 路径正确", "reset-streak`," in a),
        ("sp 有 resetStreak 函数", "const resetStreak = async" in s),
        ("sp 有解除按钮", ">解除</Button>" in s),
        ("sp 函数在 return 之前",
         s.index("const resetStreak") < s.index("<div className=\"grid gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]\">")),
    ]
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("  %-28s %s" % (n, "OK" if ok else "FAIL"))
    if bad:
        print("SELFCHECK FAIL:", bad)
        sys.exit(1)
    print("SELFCHECK PASS")


if __name__ == "__main__":
    main()
