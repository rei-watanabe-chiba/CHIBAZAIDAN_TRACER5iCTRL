"""(開発用)機器の画面を Windows の OCR(winrt)で読む。照合の見本を作るとき(tests/build_screens.py)だけが使う。

アプリは画面を見本との照合で読み(screen_match)、OCR は使わない。winrt は requirements.txt で注記にしている
(見本を作るときだけ入れる)。2026-11-25 に、消した bruker_ui.py(RemoteCTRL の窓を相手にする予備の処理)から移した。

    ocr = Ocr("ja")
    ui = read_ui(ocr, img)       # img は機器の画面を縦横 2 倍にしたもの(960 幅)。ボタンと文字を ScreenUI で返す
"""
from __future__ import annotations

import asyncio, math, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Traer5iAuto", "src"))
from PIL import Image, ImageDraw, ImageMath, ImageOps, ImageStat

from screen_ui import REF_W, ScreenUI, UIElement, detect_buttons, fix_katakana


class Ocr:
    """Windows の OCR(winrt)。イベントループを使い回し、SoftwareBitmap と DataWriter を閉じる(メモリが増えないように)"""

    def __init__(self, lang="ja"):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
        self.engine = OcrEngine.try_create_from_language(Language(lang))
        if self.engine is None:
            raise RuntimeError(f"Windows OCR 言語 '{lang}' が利用できません")
        self.loop = asyncio.new_event_loop()        # 毎回作り直すとメモリが少しずつ増えるため使い回す

    def __del__(self):
        try:
            self.loop.close()
        except Exception:
            pass

    def lines(self, img: Image.Image) -> list[tuple[str, tuple[int, int, int, int]]]:
        rgba = img.convert("RGBA")
        w, h = rgba.size
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
        dw = DataWriter()
        dw.write_bytes(rgba.tobytes("raw", "BGRA"))
        sb = SoftwareBitmap.create_copy_from_buffer(
            dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
        try:
            res = self.loop.run_until_complete(self._recognize(sb))
        finally:
            sb.close()                               # 画像バッファを即時解放
            dw.close()
        out = []
        for line in res.lines:
            words = list(line.words)
            text = _join_words([wd.text for wd in words])
            xs = [(wd.bounding_rect.x, wd.bounding_rect.y,
                   wd.bounding_rect.x + wd.bounding_rect.width,
                   wd.bounding_rect.y + wd.bounding_rect.height) for wd in words]
            x0, y0 = min(a[0] for a in xs), min(a[1] for a in xs)
            x1, y1 = max(a[2] for a in xs), max(a[3] for a in xs)
            out.append((text, (int(x0), int(y0), int(x1 - x0), int(y1 - y0))))
        return out

    async def _recognize(self, sb):
        return await self.engine.recognize_async(sb)


def _join_words(ws: list[str]) -> str:
    """日本語は1文字ずつ区切られるので、ASCII同士の間だけ空白を残す"""
    s = ""
    for w in ws:
        if s and s[-1].isascii() and s[-1].isalnum() and w[:1].isascii() and w[:1].isalnum():
            s += " "
        s += w
    return s


def read_ui(ocr: Ocr, img: Image.Image) -> ScreenUI:
    """画面を OCR で読む。青いボタンは白地に黒文字(大津の2値化)にしてから 1 つずつ読み、ほかの文字は
    ボタンを白で塗りつぶしてから全体を読む。img の幅が REF_W でなければ REF_W に合わせる"""
    k = REF_W / img.width
    if abs(k - 1) > 0.01:
        img = img.resize((REF_W, round(img.height * k)), Image.BICUBIC if k > 1 else Image.LANCZOS)
    img = img.convert("RGB")
    buttons = detect_buttons(img)
    btn_elems = []
    for (x, y, w, h) in buttons:
        gray = img.crop((x + 3, y + 3, x + w - 3, y + h - 3)).convert("L")
        t = _otsu(gray.histogram())
        bw = gray.point(lambda v: 0 if v > t else 255)
        if ImageStat.Stat(bw).mean[0] < 127:
            bw = ImageOps.invert(bw)
        text = ""
        for zoom, blur in ((1, 0), (1, 3), (2, 0), (2, 5)):   # 読めない場合は前処理を変えて再試行
            z = bw.resize((bw.width * zoom, bw.height * zoom), Image.NEAREST) if zoom > 1 else bw
            if blur:
                z = _gauss(z, blur * zoom | 1)
            text = " ".join(t for t, _ in ocr.lines(ImageOps.expand(z, 30, fill=255)))
            if text.strip():
                break
        btn_elems.append(UIElement("button", fix_katakana(text), (x, y, w, h)))
    rest = img.copy()
    d = ImageDraw.Draw(rest)
    for (x, y, w, h) in buttons:
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=(255, 255, 255))
    texts = [UIElement("text", fix_katakana(t), r) for t, r in ocr.lines(rest) if t.strip()]
    return ScreenUI(img, btn_elems, texts, 1.0)


def _otsu(hist: list[int]) -> int:
    """大津の方法で2値化のしきい値を求める(256階調のヒストグラムから)"""
    total = sum(hist)
    sum_all = sum(i * c for i, c in enumerate(hist))
    w0 = s0 = 0
    best, best_t = -1.0, 0
    for t in range(256):
        w0 += hist[t]; s0 += t * hist[t]
        w1 = total - w0
        if w0 == 0 or w1 == 0:
            continue
        m0, m1 = s0 / w0, (sum_all - s0) / w1
        v = w0 * w1 * (m0 - m1) ** 2
        if v > best:
            best, best_t = v, t
    return best_t


def _gauss(gray: Image.Image, k: int) -> Image.Image:
    """k×k のガウスぼかし(OpenCV の GaussianBlur(k, sigma=0) と同じ重み・端は折り返し)。
    Pillow の GaussianBlur は近似なので、縦横に分けて重み付きの和を自分で計算する"""
    r, sg = k // 2, 0.3 * ((k - 1) * 0.5 - 1) + 0.8
    w = [math.exp(-(i * i) / (2 * sg * sg)) for i in range(-r, r + 1)]
    w = [v / sum(w) for v in w]
    f = gray.convert("F")
    for axis in (0, 1):
        W, H = f.size
        n = W if axis == 0 else H
        ext = Image.new("F", (W + 2 * r, H) if axis == 0 else (W, H + 2 * r))
        ext.paste(f, (r, 0) if axis == 0 else (0, r))
        for i in range(1, r + 1):                     # 端は折り返し(端の画素は重ねない: OpenCV の既定)
            for src, dst in ((min(i, n - 1), r - i), (max(n - 1 - i, 0), r + n - 1 + i)):
                box = (src, 0, src + 1, H) if axis == 0 else (0, src, W, src + 1)
                ext.paste(f.crop(box), (dst, 0) if axis == 0 else (0, dst))
        acc = None
        for j, wj in enumerate(w):
            part = ext.crop((j, 0, j + W, H) if axis == 0 else (0, j, W, j + H))
            acc = (ImageMath.lambda_eval(lambda a, wj=wj: a["p"] * wj, p=part) if acc is None else
                   ImageMath.lambda_eval(lambda a, wj=wj: a["s"] + a["p"] * wj, s=acc, p=part))
        f = acc
    return f.point(lambda v: v + 0.5).convert("L")   # 四捨五入して 8 ビットへ
