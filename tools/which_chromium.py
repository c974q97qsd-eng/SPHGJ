# -*- coding: utf-8 -*-
"""打印当前 playwright 期望的 chromium / headless-shell revision(供 build.bat 用)。

输出: <chromium_rev>|<headless_shell_rev>   例: 1223|1223
失败输出空值,由 build.bat 报错。

为什么需要它: 本机 ms-playwright 目录里可能同时有多个版本(1208/1223/1234),
按目录名取"最新"会把比当前 playwright 更新的版本拷进包,exe 启动时
找不到它期望的 revision 会直接失败。必须以 playwright 自带 browsers.json 为准。
"""
import json
import os
import sys


def main():
    try:
        import playwright
        base = os.path.dirname(os.path.abspath(playwright.__file__))
        p = os.path.join(base, 'driver', 'package', 'browsers.json')
        data = json.load(open(p, encoding='utf-8'))
        rev = {}
        for b in data.get('browsers', []):
            rev[b.get('name')] = str(b.get('revision') or '')
        print('%s|%s' % (rev.get('chromium', ''), rev.get('chromium-headless-shell', '')))
    except Exception as e:                                   # noqa: BLE001
        sys.stderr.write('which_chromium failed: %s\n' % e)
        print('|')


if __name__ == '__main__':
    main()
