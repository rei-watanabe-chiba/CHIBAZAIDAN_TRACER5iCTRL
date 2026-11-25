"""Bruker RemoteCTRL 画面の 特定 -> キャプチャ -> UI取得 モジュール(入力は一切送らない)

    python bruker_ui.py            # 現在の画面を解析して一覧表示、注釈画像とJSONを保存
    python bruker_ui.py --watch 2  # 2秒ごとに画面が変わったら再解析

ライブラリとして:
    from bruker_ui import RemoteCtrl
    rc = RemoteCtrl()
    ui = rc.read_ui()
    ui.find_button("スペクトル")   # -> UIElement or None
    ui.has_text("計数率")          # -> bool
"""
from __future__ import annotations

import asyncio, ctypes, ctypes.wintypes as wt, json, os, re, sys, time
from dataclasses import dataclass, asdict, field

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat   # 画像処理は Pillow だけ(配布を軽くする)

from storage import DIAG_DIR, use_base


user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

WINDOW_CLASS = "S1RemoteHostClass"
REF_W = 960          # 解析の基準幅(この幅に正規化してからボタン検出・OCRを行う)
NATIVE_W = 480       # 機器の画面の幅(RemoteCTRL の論理px、ズーム 1X)。窓を広げても画面は左上に固定の大きさで、
                     # 右・下に白い余白が増えるだけ(2026-11-10 実機で確認)。窓の幅ではなくこの幅を基準にする
OUT_DIR = DIAG_DIR                    # config/remotectrl_test(診断用の画像。VBS のフォルダ基準)


# ---------------------------------------------------------------- データ構造
@dataclass
class UIElement:
    kind: str                      # "button" / "text"
    text: str
    rect: tuple[int, int, int, int]  # クライアント座標 (x, y, w, h)

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return x + w // 2, y + h // 2


@dataclass
class ScreenUI:
    image: Image.Image = field(repr=False)
    buttons: list[UIElement]
    texts: list[UIElement]
    scale: float                   # 機器の画面の幅(キャプチャのpx) / 基準幅(REF_W)。座標は全て基準座標系

    def find_any(self, label: str, min_score: float = 0.6) -> UIElement | None:
        """ボタン・テキストの両方から探す(リスト項目の選択などに使う)"""
        scored = [(_match_score(label, e.text), e) for e in self.buttons + self.texts]
        scored = [s for s in scored if s[0] >= min_score]
        return max(scored, key=lambda s: s[0])[1] if scored else None

    def find_button(self, label: str, min_score: float = 0.5) -> UIElement | None:
        """ラベルでボタンを探す。完全一致/部分一致を優先し、なければ類似度で探す
        (OCR誤認識 例: '設定'->'似定' にも対応)"""
        scored = [(_match_score(label, b.text), b) for b in self.buttons]
        scored = [s for s in scored if s[0] >= min_score]
        return max(scored, key=lambda s: s[0])[1] if scored else None

    def has_text(self, s: str) -> bool:
        key = _norm(s)
        return any(key in _norm(e.text) for e in self.buttons + self.texts)

    def to_dict(self):
        return {"scale": self.scale,
                "buttons": [asdict(b) | {"center": b.center} for b in self.buttons],
                "texts": [asdict(t) for t in self.texts]}


_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "|": "l", "i": "l", "5": "s"})


def _norm(s: str) -> str:
    """照合用の正規化: 空白除去・小文字化・OCRで取り違えやすい文字の統一 (0/O, 1/l/i, 5/S)"""
    return _fix_katakana(s).replace(" ", "").replace("　", "").lower().translate(_CONFUSABLE)


def same_name(a: str, b: str) -> bool:
    """名前の一致判定(b に a が含まれるか。OCR誤認識を吸収)
    完全な部分一致がなければ、b 内の同じ長さの区間と比べて9割程度一致すれば同一とみなす"""
    from difflib import SequenceMatcher
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb:
        return True
    if any(c.isdigit() for c in a) or len(na) < 6:
        return False      # 数字入り(例: Precious Metals 1/2)や短い名前は誤一致が危険なので厳密一致のみ
    L = len(na)
    best = max(SequenceMatcher(None, na, nb[i:i + L]).ratio() for i in range(max(1, len(nb) - L + 1)))
    return best >= 0.8                          # (6文字未満は上で厳密一致のみにしている)


def _match_score(label: str, text: str) -> float:
    from difflib import SequenceMatcher
    key, cand = _norm(label), _norm(text)
    if not key or not cand:
        return 0.0
    if key == cand:
        return 1.0
    parts = [_norm(p) for p in text.split(" ") if p.strip()]
    if key in parts:
        return 0.95
    if key in cand:
        return 0.9
    return max(SequenceMatcher(None, key, p).ratio() for p in parts + [cand]) * 0.85


def _fix_katakana(s: str) -> str:
    """カタカナの後のハイフン類を長音記号に統一 (OCRの誤認識対策)"""
    out = []
    for i, c in enumerate(s):
        if c in "-‐－―─" and i > 0 and "゠" <= s[i - 1] <= "ヿ":
            c = "ー"
        out.append(c)
    return "".join(out)


# ---------------------------------------------------------------- OCR (Windows 標準)
class _Ocr:
    """Windows の OCR(winrt)。アプリ(機器へ直接つなぐ)は使わない。見本を作るとき(tests/build_screens.py)と、
    予備の RemoteCTRL の窓を相手にする方式(--remotectrl)だけが使う。winrt はここで初めて読み込む(配布物には入れない)"""

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


# ---------------------------------------------------------------- RemoteCTRL のメニュー(予備: --remotectrl のとき)
user32.GetMenu.restype = wt.HMENU
user32.GetMenu.argtypes = [wt.HWND]
user32.GetMenuStringW.argtypes = [wt.HMENU, wt.UINT, wt.LPWSTR, ctypes.c_int, wt.UINT]
CMD_TRIGGER = 40011                        # メニュー「Trigger Pull/Release ( F6 )」(文字で状態がわかる)


def trigger_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetMenuStringW(user32.GetMenu(hwnd), CMD_TRIGGER, buf, 128, 0)   # MF_BYCOMMAND
    return buf.value


def _print_window(hwnd: int, flags: int) -> Image.Image:
    r = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(r))
    W, H = r.right, r.bottom
    hdc = user32.GetDC(hwnd); gdi32 = ctypes.windll.gdi32
    mdc = gdi32.CreateCompatibleDC(hdc); bmp = gdi32.CreateCompatibleBitmap(hdc, W, H)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, flags)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("a", wt.LONG), ("b", wt.LONG), ("c", wt.DWORD), ("d", wt.DWORD)]
    bih = BIH(ctypes.sizeof(BIH), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(W * H * 4)
    gdi32.GetDIBits(mdc, bmp, 0, H, buf, ctypes.byref(bih), 0)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(mdc); user32.ReleaseDC(hwnd, hdc)
    return Image.frombuffer("RGB", (W, H), buf, "raw", "BGRX", 0, 1)


# ---------------------------------------------------------------- 本体
class RemoteCtrl:
    def __init__(self, ocr_lang="ja"):
        self.hwnd = self.find_window()
        self.ocr = _Ocr(ocr_lang)

    # 1. 画面の特定
    @staticmethod
    def find_window() -> int:
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        if not hwnd:
            raise RuntimeError("Bruker RemoteCTRL の窓が見つかりません(起動・接続を確認)")
        return hwnd

    def client_rect(self) -> tuple[int, int, int, int]:
        r = wt.RECT(); user32.GetClientRect(self.hwnd, ctypes.byref(r))
        p = wt.POINT(0, 0); user32.ClientToScreen(self.hwnd, ctypes.byref(p))
        return p.x, p.y, p.x + r.right, p.y + r.bottom

    # 2. キャプチャ
    def capture_background(self) -> Image.Image:
        """前面に出さずに取得(PrintWindow)。RemoteCTRLはDPI非対応のため
        等倍で描かれる左上部分を切り出し、クライアントサイズへ拡大する"""
        img = _print_window(self.hwnd, 1 | 2)
        W, H = img.size
        s = self._dpi_scale()
        if s > 1.01:
            img = img.crop((0, 0, round(W / s), round(H / s))).resize((W, H), Image.NEAREST)
        return img

    def _dpi_scale(self) -> float:
        """DPI 非対応の RemoteCTRL を拡大して表示している倍率(拡大していなければ 1)"""
        s = user32.GetDpiForSystem() / 96.0
        return s if s > 1.01 and user32.GetDpiForWindow(self.hwnd) == 96 else 1.0

    def content_width(self) -> int:
        """キャプチャの中の機器の画面の幅(px)。窓の大きさによらず一定"""
        return round(NATIVE_W * self._dpi_scale())

    # 入力・トリガー(auto_measure.Operator から使う。device_link.DirectRemote も同じ名前で持つ)
    def prepare(self):
        """読む前の準備: RemoteCTRL を起動し直していたら窓を探し直し、最小化なら(前面に出さずに)元に戻す"""
        if not user32.IsWindow(self.hwnd):
            self.hwnd = self.find_window()
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, 4)          # SW_SHOWNOACTIVATE(他の窓の後ろに隠れているだけなら何もしない)
            time.sleep(0.5)

    def click(self, x: int, y: int):
        """クライアント座標 (x, y) を押して離す。窓へ直接メッセージを送る(前面化不要・実マウスを動かさない)"""
        lp = (y << 16) | (x & 0xFFFF)
        user32.PostMessageW(self.hwnd, 0x0201, 0x0001, lp); time.sleep(0.08)    # WM_LBUTTONDOWN
        user32.PostMessageW(self.hwnd, 0x0202, 0x0000, lp)                      # WM_LBUTTONUP

    def trigger_state(self) -> str:
        """'pull'(引ける=待機) / 'release'(引いている) / 'unknown'(メニューの文字で判断。埋め込み中は保存したメニュー)"""
        t = trigger_text(self.hwnd).lower()
        return "pull" if "pull" in t else "release" if "release" in t else "unknown"

    def trigger(self):
        """メニュー「Trigger Pull/Release ( F6 )」を実行(キー入力と違い、前面化不要で確実に届く)"""
        user32.PostMessageW(self.hwnd, 0x0111, CMD_TRIGGER, 0)                  # WM_COMMAND

    # 3. UI取得
    def to_client(self, ui: ScreenUI, pt: tuple[int, int]) -> tuple[int, int]:
        """基準座標 -> クライアント座標。機器の画面は左上に固定の大きさなので、窓の大きさによらず倍率だけで決まる"""
        return round(pt[0] * ui.scale), round(pt[1] * ui.scale)

    def read_ui(self, img: Image.Image | None = None, content_w: int | None = None) -> ScreenUI:
        """画面解析。キャプチャから機器の画面(左上の固定幅)だけを切り出し、幅 REF_W に正規化してから解析する
        (窓を広げたときの右の余白は解析しない)。content_w を省くと、窓から求める(窓が無ければ画像の幅)"""
        img = img or self.capture_background()
        W, H = img.size
        cw = content_w or (self.content_width() if getattr(self, "hwnd", None) else W)
        if W > cw:
            img = img.crop((0, 0, cw, H))                 # 右の余白を落とす(下の余白は白いだけなので残す)
        k = REF_W / cw
        if abs(k - 1) > 0.01:
            img = img.resize((round(img.width * k), round(H * k)), Image.BICUBIC if k > 1 else Image.LANCZOS)
        img = img.convert("RGB")
        buttons = _detect_buttons(img)
        btn_elems = []
        for (x, y, w, h) in buttons:
            # 青地に白文字 -> 白地に黒文字へ変換してOCR精度を上げる(大津の2値化)
            gray = img.crop((x + 3, y + 3, x + w - 3, y + h - 3)).convert("L")
            t = _otsu(gray.histogram())
            bw = gray.point(lambda v: 0 if v > t else 255)
            if _mean(bw) < 127:
                bw = ImageOps.invert(bw)
            text = ""
            for zoom, blur in ((1, 0), (1, 3), (2, 0), (2, 5)):   # 読めない場合は前処理を変えて再試行
                z = bw.resize((bw.width * zoom, bw.height * zoom), Image.NEAREST) if zoom > 1 else bw
                if blur:
                    z = _gauss(z, blur * zoom | 1)
                pad = ImageOps.expand(z, 30, fill=255)
                text = " ".join(t for t, _ in self.ocr.lines(pad))
                if text.strip():
                    break
            text = _fix_katakana(text)
            btn_elems.append(UIElement("button", text, (x, y, w, h)))

        # ボタン以外の文字: ボタン領域を白で塗りつぶしてから全体OCR
        rest = img.copy()
        d = ImageDraw.Draw(rest)
        for (x, y, w, h) in buttons:
            d.rectangle((x, y, x + w - 1, y + h - 1), fill=(255, 255, 255))
        texts = [UIElement("text", _fix_katakana(t), r) for t, r in self.ocr.lines(rest) if t.strip()]
        return ScreenUI(img, btn_elems, texts, cw / REF_W)


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


def _mean(gray: Image.Image) -> float:
    return ImageStat.Stat(gray).mean[0]


def _sigma(k: int) -> float:
    """ぼかしの大きさ k(奇数)に対応する標準偏差(OpenCV の GaussianBlur で sigma=0 のときと同じ式)"""
    return 0.3 * ((k - 1) * 0.5 - 1) + 0.8


def _gauss(gray: Image.Image, k: int) -> Image.Image:
    """k×k のガウスぼかし(OpenCV の GaussianBlur(k, sigma=0) と同じ重み・端は折り返し)。
    Pillow の GaussianBlur は近似なので、縦横に分けて重み付きの和を自分で計算する"""
    import math
    from PIL import ImageMath
    r, sg = k // 2, _sigma(k)
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


def _components(mask: Image.Image) -> list[tuple[int, int, int, int, int]]:
    """2値画像(0/255)の白いかたまり(8近傍でつながる)ごとに (x, y, w, h, 面積) を返す。
    行ごとの白の連なり(ラン)を上の行のランとつなげて求める(画素ごとに処理しないので速い)"""
    W, H = mask.size
    data = mask.tobytes()
    parent: list[int] = []
    box: list[list[int]] = []          # [x0, y0, x1, y1, area]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev: list[tuple[int, int, int]] = []   # 上の行のラン (開始, 終了(含まない), 番号)
    run = re.compile(rb"\xff+")
    for y in range(H):
        cur = []
        for m in run.finditer(data, y * W, (y + 1) * W):
            a, b = m.start() - y * W, m.end() - y * W
            i = len(parent); parent.append(i); box.append([a, y, b - 1, y, b - a])
            for pa, pb, j in prev:
                if pa <= b and pb >= a:             # 8近傍: 斜めに接していてもつながる
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
                        bi, bj = box[ri], box[rj]
                        bi[0], bi[1] = min(bi[0], bj[0]), min(bi[1], bj[1])
                        bi[2], bi[3] = max(bi[2], bj[2]), max(bi[3], bj[3])
                        bi[4] += bj[4]
            cur.append((a, b, i))
        prev = cur
    return [(b[0], b[1], b[2] - b[0] + 1, b[3] - b[1] + 1, b[4]) for i, b in enumerate(box) if find(i) == i]


def _detect_buttons(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """青色(機器UIのボタン色)の矩形領域を検出"""
    r, g, b = img.split()
    mask = ImageChops.darker(ImageChops.darker(r.point(lambda v: 255 if v < 90 else 0),
                                               g.point(lambda v: 255 if v < 90 else 0)),
                             b.point(lambda v: 255 if v > 170 else 0))
    # 文字の穴を埋める(9x9 の膨張 -> 収縮)。箱型ぼかしの平均で膨張・収縮を速く求める
    mask = mask.filter(ImageFilter.BoxBlur(4)).point(lambda v: 255 if v > 0 else 0)
    mask = mask.filter(ImageFilter.BoxBlur(4)).point(lambda v: 255 if v >= 255 else 0)
    W, H = mask.size
    rects = []
    for x, y, w, h, area in _components(mask):
        if w < 40 or h < 30 or area < 0.6 * w * h:   # 小さい/矩形でないものは除外
            continue
        if w > 0.95 * W and h > 0.5 * H:              # 画面全体のような領域は除外
            continue
        rects.append((x, y, w, h))
    return sorted(rects, key=lambda t: (t[1] // 20, t[0]))


def annotate(ui: ScreenUI, path: str):
    a = ui.image.convert("RGB")
    d = ImageDraw.Draw(a)
    for i, b in enumerate(ui.buttons):
        x, y, w, h = b.rect
        d.rectangle((x, y, x + w, y + h), outline=(255, 0, 0), width=3)
        d.text((x + 4, y + 4), f"B{i}", fill=(255, 0, 0))
    for t in ui.texts:
        x, y, w, h = t.rect
        d.rectangle((x, y, x + w, y + h), outline=(0, 160, 0), width=2)
    a.save(path)


def _report(rc: RemoteCtrl, ui: ScreenUI, tag=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    ui.image.save(os.path.join(OUT_DIR, f"ui{tag}_raw.png"))
    annotate(ui, os.path.join(OUT_DIR, f"ui{tag}_annotated.png"))
    with open(os.path.join(OUT_DIR, f"ui{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(ui.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"窓 hwnd={rc.hwnd:#x}  画像 {ui.image.size}  DPI倍率 {ui.scale}")
    print("--- ボタン ---")
    for i, b in enumerate(ui.buttons):
        print(f"B{i:<2} {b.text!r:24} rect={b.rect} center={b.center}")
    print("--- テキスト ---")
    for t in ui.texts:
        print(f"    {t.text!r:40} rect={t.rect}")
    print(f"保存先: {OUT_DIR}")


if __name__ == "__main__":
    use_base()
    rc = RemoteCtrl()
    if "--watch" in sys.argv:
        interval = float(sys.argv[sys.argv.index("--watch") + 1])
        last = None
        while True:
            img = rc.capture_background()
            small = img.convert("RGB").resize((120, 165))
            if last is None or sum(ImageStat.Stat(ImageChops.difference(small, last)).mean) / 3 > 2:
                print(time.strftime("\n[%H:%M:%S] 画面変化"))
                _report(rc, rc.read_ui(img))
                last = small
            time.sleep(interval)
    else:
        _report(rc, rc.read_ui())
