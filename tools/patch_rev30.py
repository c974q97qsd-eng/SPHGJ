# -*- coding: utf-8 -*-
"""rev30 一次性补丁: 二维码截图自动裁掉四周深色背景。

现象(2026-10-20 21:56 用户截图): 弹窗里二维码左右两侧 + 下方各有一条深色带。
实测后端推送的图 (data/_cloud_sync/qr_now.png) 是 208x208, 但二维码白底方块
只占中间 160x160, 四周是微信登录页的深色背景 —— iframe 的 bounding_box 比
二维码本身大一圈, 直接 clip 就把背景带进来了。

修法: 截图后用 canvas 从边缘向内收缩到"白底边界", 只留白底方块。
本地实测 208x208 -> [left=22, top=0, 160x160], 裁剪后是干净的纯二维码。
裁不出来(尺寸异常/页面 CSP 拦 data:)就原样返回, 不影响可用性。

前端同步: 不再强制 w/h 180(会把 160 放大糊掉), 改按图片自然尺寸显示。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TRIM_JS = r'''() => `async (b64) => {
            const img = new Image();
            img.src = 'data:image/png;base64,' + b64;
            await img.decode();
            const w = img.naturalWidth, h = img.naturalHeight;
            if (!w || !h) return null;
            const c = document.createElement('canvas');
            c.width = w; c.height = h;
            const g = c.getContext('2d');
            g.drawImage(img, 0, 0);
            const d = g.getImageData(0, 0, w, h).data;
            const isWhite = (x, y) => {
                const i = (y * w + x) * 4;
                return d[i] > 190 && d[i + 1] > 190 && d[i + 2] > 190;
            };
            const rowWhite = (y) => { let n = 0; for (let x = 0; x < w; x++) if (isWhite(x, y)) n++; return n / w; };
            const colWhite = (x) => { let n = 0; for (let y = 0; y < h; y++) if (isWhite(x, y)) n++; return n / h; };
            let top = 0, bottom = h - 1, left = 0, right = w - 1;
            while (top < bottom && rowWhite(top) < 0.5) top++;
            while (bottom > top && rowWhite(bottom) < 0.5) bottom--;
            while (left < right && colWhite(left) < 0.5) left++;
            while (right > left && colWhite(right) < 0.5) right--;
            const nw = right - left + 1, nh = bottom - top + 1;
            if (nw < 60 || nh < 60) return null;
            if (nw === w && nh === h) return null;
            const c2 = document.createElement('canvas');
            c2.width = nw; c2.height = nh;
            c2.getContext('2d').drawImage(c, left, top, nw, nh, 0, 0, nw, nh);
            return c2.toDataURL('image/png').split(',')[1];
        }`'''


PATCHES = {
 "backend/login_capture.py": [
  # 1) 新增裁剪方法（插在 _capture_qr 定义之前，作为独立方法）
  (
'''    async def _capture_qr(self):
        """获取二维码图片(三级策略:网络拦截原图 > iframe clip > 整页 fallback)。''',
'''    async def _trim_qr_dark(self, b64_png):
        """裁掉二维码截图四周的深色背景(rev30)。返回 None 表示保持原样。

        iframe 的 bounding_box 比二维码本身大一圈: 微信登录页 iframe 是 208x208,
        真正的白底二维码只占中间 160x160, 四周是页面深色背景 —— 直接 clip 出来
        就是用户看到的"左右两条 + 下面一条黑边"。
        这里在浏览器里用 canvas 读像素, 从四边向内收缩到白底边界再裁:
        逐行/逐列统计白色像素占比, <50% 就认为还是背景, 一直收缩到白底区。
        实测 208x208 -> (left=22, top=0, 160x160), 裁完是干净纯二维码。
        任何异常(页面已关/CSP 拦 data:/尺寸太小)都返回 None, 调用方用原图。
        """
        if not self.page:
            return None
        try:
            js = """async (b64) => {
                const img = new Image();
                img.src = 'data:image/png;base64,' + b64;
                await img.decode();
                const w = img.naturalWidth, h = img.naturalHeight;
                if (!w || !h) return null;
                const c = document.createElement('canvas');
                c.width = w; c.height = h;
                const g = c.getContext('2d');
                g.drawImage(img, 0, 0);
                const d = g.getImageData(0, 0, w, h).data;
                const isWhite = (x, y) => {
                    const i = (y * w + x) * 4;
                    return d[i] > 190 && d[i+1] > 190 && d[i+2] > 190;
                };
                const rowWhite = (y) => { let n = 0; for (let x = 0; x < w; x++) if (isWhite(x, y)) n++; return n / w; };
                const colWhite = (x) => { let n = 0; for (let y = 0; y < h; y++) if (isWhite(x, y)) n++; return n / h; };
                let top = 0, bottom = h - 1, left = 0, right = w - 1;
                while (top < bottom && rowWhite(top) < 0.5) top++;
                while (bottom > top && rowWhite(bottom) < 0.5) bottom--;
                while (left < right && colWhite(left) < 0.5) left++;
                while (right > left && colWhite(right) < 0.5) right--;
                const nw = right - left + 1, nh = bottom - top + 1;
                if (nw < 60 || nh < 60) return null;
                if (nw === w && nh === h) return null;
                const c2 = document.createElement('canvas');
                c2.width = nw; c2.height = nh;
                c2.getContext('2d').drawImage(c, left, top, nw, nh, 0, 0, nw, nh);
                return c2.toDataURL('image/png').split(',')[1];
            }"""
            out = await self.page.evaluate(js, b64_png)
            if out:
                logger.info(f"[login:{self.sid}] 已裁掉二维码四周深色背景")
                return out
        except Exception as e:
            logger.debug(f"[login:{self.sid}] 裁剪二维码背景失败(用原图): {e}")
        return None

    async def _capture_qr(self):
        """获取二维码图片(三级策略:网络拦截原图 > iframe clip > 整页 fallback)。''',
   "新增 _trim_qr_dark"),
  # 2) iframe clip 结果过裁剪
  (
'''                    png = await self.page.screenshot(clip=box, timeout=5000)
                    if len(png) > 500:
                        logger.info(f"[login:{self.sid}] 二维码命中 iframe clip[{i}] {box} bytes={len(png)}")
                        return "data:image/png;base64," + base64.b64encode(png).decode()''',
'''                    png = await self.page.screenshot(clip=box, timeout=5000)
                    if len(png) > 500:
                        logger.info(f"[login:{self.sid}] 二维码命中 iframe clip[{i}] {box} bytes={len(png)}")
                        b64 = base64.b64encode(png).decode()
                        b64 = await self._trim_qr_dark(b64) or b64
                        return "data:image/png;base64," + b64''',
   "iframe clip 过裁剪"),
  # 3) 选择器结果过裁剪
  (
'''                    png = await loc.screenshot(timeout=3000)
                    if len(png) > 500:
                        logger.info(f"[login:{self.sid}] 二维码命中选择器: {sel}")
                        return "data:image/png;base64," + base64.b64encode(png).decode()''',
'''                    png = await loc.screenshot(timeout=3000)
                    if len(png) > 500:
                        logger.info(f"[login:{self.sid}] 二维码命中选择器: {sel}")
                        b64 = base64.b64encode(png).decode()
                        b64 = await self._trim_qr_dark(b64) or b64
                        return "data:image/png;base64," + b64''',
   "选择器结果过裁剪"),
  # 4) fallback 裁剪结果也过一次
  (
'''                if len(png) > 1000:
                    logger.info(f"[login:{self.sid}] 整页 fallback 裁剪出二维码 {box} bytes={len(png)}")
                    return "data:image/png;base64," + base64.b64encode(png).decode()''',
'''                if len(png) > 1000:
                    logger.info(f"[login:{self.sid}] 整页 fallback 裁剪出二维码 {box} bytes={len(png)}")
                    b64 = base64.b64encode(png).decode()
                    b64 = await self._trim_qr_dark(b64) or b64
                    return "data:image/png;base64," + b64''',
   "fallback 结果过裁剪"),
 ],

 "frontend/src/components/accounts/AddAccountDialog.tsx": [
  (
'''                  <img
                    src={qrImage}
                    alt="扫码登录"
                    className="w-[180px] h-[180px] rounded-md bg-white object-contain"
                  />''',
'''                  <img
                    src={qrImage}
                    alt="扫码登录"
                    className="max-h-[190px] max-w-[190px] w-auto h-auto rounded-md bg-white"
                  />''',
   "二维码按自然尺寸显示"),
 ],
}


def main():
    for rel, items in PATCHES.items():
        path = os.path.join(ROOT, rel)
        src = io.open(path, encoding="utf-8").read()
        print("== %s ==" % rel)
        for old, new, tag in items:
            n = src.count(old)
            if n != 1:
                raise SystemExit("FAIL 命中 %d 次(需恰好 1): %s -> %s" % (n, tag, rel))
            src = src.replace(old, new, 1)
            print("  OK  %s" % tag)
        io.open(path, "w", encoding="utf-8", newline="").write(src)

    print("\n== 自校验 ==")
    checks = [
        ("backend/login_capture.py", "async def _trim_qr_dark"),
        ("backend/login_capture.py", "b64 = await self._trim_qr_dark(b64) or b64"),
        ("frontend/src/components/accounts/AddAccountDialog.tsx", "max-h-[190px] max-w-[190px]"),
    ]
    bad = 0
    for rel, needle in checks:
        s = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        cnt = s.count(needle)
        want = 3 if needle == "b64 = await self._trim_qr_dark(b64) or b64" else 1
        ok = cnt == want
        print("  %s  出现 %d 次(期望 %d)  %s" % ("PASS" if ok else "FAIL", cnt, want, needle[:40]))
        if not ok:
            bad += 1
    print("\nSELFCHECK", "PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


sys.exit(main())
