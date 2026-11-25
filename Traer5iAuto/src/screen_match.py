"""機器の画面を、見本の絵と照合して読む(OCR を使わない)。直接つないだ機器の画面(480x640)用

機器の画面は配置が決まっていて、文字も毎回同じ画素で描かれるので、見本と画素で比べれば読める:
  - ボタン: 青いかたまりを見つけ(bruker_ui._detect_buttons)、同じ位置・大きさの見本と内側(枠の点線を除く)を比べて名前を決める
  - 文字: 見本の位置の絵を比べる(決まった文字だけ。状態の行=上の 25px は時刻などで変わるので使わない)
  - 数字: 0〜9 の字形(テンキーの表示欄に打って集めた)と1文字ずつ比べる。時間の欄([-][+] の間)と、見本で決めた数字の欄
見本は src/screens/(画面の PNG と screens.json)。作り方は tests/build_screens.py。
戻り値は bruker_ui.ScreenUI(座標は基準の 960 幅)なので、auto_measure の画面判定・クリックはそのまま使える。
"""
from __future__ import annotations

import json, os

from PIL import Image, ImageChops, ImageStat

from bruker_ui import REF_W, ScreenUI, UIElement, _detect_buttons

HERE = os.path.dirname(os.path.abspath(__file__))
SCREENS = os.path.join(HERE, "screens")
W, H = 480, 640
K = REF_W // W                      # 機器の px → 基準座標(960 幅)
INSET = 5                           # ボタンを比べるとき内側だけにする幅(フォーカスの点線の枠を除く)
DIFF_MAX = 6.0                      # 見本と同じとみなす、画素の差の平均(0〜255)
SPACE_GAP = 6                       # 数字の行: 字の間がこれより空いていたら空白とみなす(px)
NEAR = 3                            # 見つけたボタンの枠と見本の枠の、位置・大きさの違いの許し(px)
DARK = 110                          # 数字の字形: これより暗い画素を字とみなす
STATUS_H = 25                       # 上の状態の行(時刻・電池などで変わる。照合に使わない)


def _diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 999.0
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _inner(img: Image.Image, r) -> Image.Image:
    l, t, w, h = r
    return img.crop((l + INSET, t + INSET, l + w - INSET, t + h - INSET))


class Library:
    """見本(screens.json と画面の PNG)。items: {kind: "button"|"text"|"number", label, rect(機器の px: x,y,w,h), screen}"""

    def __init__(self, folder: str = SCREENS):
        with open(os.path.join(folder, "screens.json"), encoding="utf-8") as f:
            d = json.load(f)
        refs = {n: Image.open(os.path.join(folder, n + ".png")).convert("RGB") for n in d["screens"]}
        self.buttons, self.texts, self.numbers = [], [], []
        for it in d["items"]:
            img, r = refs[it["screen"]], tuple(it["rect"])
            if it["kind"] == "button":
                self.buttons.append((it["label"], r, _inner(img, r)))
            elif it["kind"] == "text":
                self.texts.append((it["label"], r, img.crop((r[0], r[1], r[0] + r[2], r[1] + r[3]))))
            else:
                self.numbers.append((it.get("label", ""), r))        # label: 前に付ける文字(例「時間」)
        g = d["digits"]                                  # 0〜9 の字形(テンキーの表示欄から)
        self.glyphs = [(ch, _bits(refs[g["screen"]].crop(tuple(box)))) for ch, box in zip(g["chars"], g["boxes"])]
        for e in d.get("glyphs", []):                    # そのほかの字形(結果の画面の - : .)
            self.glyphs.append((e["char"], _bits(refs[e["screen"]].crop(tuple(e["box"])))))

    # ---- 数字
    def read_number(self, img: Image.Image, r) -> str:
        """枠 r(機器の px: x,y,w,h)の中の数字を読む(読めなければ "")"""
        l, t, w, h = r
        out, prev = [], None
        for box in _glyph_boxes(img.crop((l, t, l + w, t + h))):
            bits = _bits(img.crop((l + box[0], t + box[1], l + box[2], t + box[3])))
            best = min(self.glyphs, key=lambda g: _bits_diff(g[1], bits))
            if _bits_diff(best[1], bits) > 0.25:
                return ""                                 # 知らない字(数字・記号以外)
            if prev is not None and box[0] - prev > SPACE_GAP:
                out.append(" ")
            out.append(best[0])
            prev = box[2]
        return "".join(out)

    # ---- 画面
    def read(self, frame: Image.Image) -> ScreenUI:
        """機器の画面(480x640)を読む。座標は基準(960 幅)"""
        frame = frame.convert("RGB")
        big = frame.resize((W * K, H * K), Image.NEAREST)
        buttons = []
        for (x, y, w, h) in _detect_buttons(big):
            r = (x // K, y // K, w // K, h // K)
            # フォーカスのあるボタンは、見つかる枠が 1px ずつ大きい(2026-11-10 実機)。位置・大きさが近い見本を、
            # 見本の位置で切り出して比べる(見つけた枠ではなく)
            cands = [(lab, _diff(_inner(frame, rr), tmpl)) for lab, rr, tmpl in self.buttons
                     if all(abs(a - b) <= NEAR for a, b in zip(rr, r))]
            cands = [c for c in cands if c[1] <= DIFF_MAX]
            label = min(cands, key=lambda c: c[1])[0] if cands else ""
            buttons.append(UIElement("button", label, (x, y, w, h)))
        texts = []
        for lab, (x, y, w, h), tmpl in self.texts:
            if _diff(frame.crop((x, y, x + w, y + h)), tmpl) <= DIFF_MAX:
                texts.append(UIElement("text", lab, (x * K, y * K, w * K, h * K)))
        for prefix, r in self.numbers + [("", b) for b in _time_field_boxes(buttons)]:
            v = self.read_number(frame, r)
            if v:
                texts.append(UIElement("text", f"{prefix} {v}" if prefix else v, tuple(c * K for c in r)))
        return ScreenUI(big, buttons, texts, 1.0)


def _time_field_boxes(buttons) -> list:
    """時間の欄([-] と [+] の小さいボタンの間)の中(機器の px)"""
    small = sorted((b for b in buttons if b.rect[2] < 120 and b.rect[3] < 120), key=lambda b: b.rect[1])
    rows = []
    for b in small:
        if rows and abs(rows[-1][0].rect[1] - b.rect[1]) < 30:
            rows[-1].append(b)
        else:
            rows.append([b])
    out = []
    for r in rows:
        if len(r) == 2:
            m, p = sorted(r, key=lambda b: b.rect[0])
            x0, x1 = (m.rect[0] + m.rect[2]) // K, p.rect[0] // K
            out.append((x0 + 4, m.rect[1] // K + 4, x1 - x0 - 8, m.rect[3] // K - 8))
    return out


def _bits(img: Image.Image) -> list[list[bool]]:
    g = img.convert("L")
    return [[g.getpixel((x, y)) < DARK for x in range(g.width)] for y in range(g.height)]


def _bits_diff(a, b) -> float:
    """字形の違い(0 = 同じ)。大きさが違えば 1"""
    if len(a) != len(b) or len(a[0]) != len(b[0]):
        return 1.0
    n = sum(x != y for ra, rb in zip(a, b) for x, y in zip(ra, rb))
    return n / (len(a) * len(a[0]))


def _glyph_boxes(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """暗い画素の列のまとまりごとに、字の外枠(x0, y0, x1, y1)。欄の枠の線(端から端まで暗い行・列)は除く"""
    b = _bits(img)
    h = len(b)
    w = len(b[0]) if h else 0
    rows = [y for y in range(h) if 0 < sum(b[y]) < w * 0.8]
    cols = [x for x in range(w) if any(b[y][x] for y in rows) and sum(b[y][x] for y in range(h)) < h * 0.8]
    groups: list[list[int]] = []
    for x in cols:
        if groups and x == groups[-1][-1] + 1:
            groups[-1].append(x)
        else:
            groups.append([x])
    boxes = []
    for g in groups:
        ys = [y for y in rows if any(b[y][c] for c in g)]
        boxes.append((g[0], min(ys), g[-1] + 1, max(ys) + 1))
    return boxes


BIG = (60, 40)                      # 名前の無いボタンがこれ以上の大きさ(機器の px)なら、読めていないとみなす


def recognized(ui: ScreenUI) -> bool:
    """見本で読めた画面か: 名前の付いたボタンか決まった文字があり、しかも名前の無い大きなボタンが無い。
    一部のボタンだけ読めないとき(フォーカスの枠など)に、読めたことにして「ボタンが見つかりません」で止まらないように"""
    if any(not b.text and b.rect[2] >= BIG[0] * K and b.rect[3] >= BIG[1] * K for b in ui.buttons):
        return False
    return any(b.text for b in ui.buttons) or any(not t.text.replace(".", "").isdigit() for t in ui.texts)


# ---- 測定中・結果画面の題名(上の欄。ふつうはモードの名前。照射対象なしなどでは知らせの文になる)
TITLE_BOX = (4, 30, 476, 67)        # 題名の欄(機器の px)


def title_crop(frame: Image.Image) -> Image.Image:
    return frame.convert("RGB").crop(TITLE_BOX)


_TITLES: dict[str, Image.Image] | None = None


def title_samples() -> dict[str, Image.Image]:
    """結果画面の題名の見本: {モード名: 絵}(screens/titles/<モード>.png。結果画面から切り出す。測定中の画面の題名はモードの名前と
    限らないので使わない: No Results / GlassTrace / 取得 / スペクトル。Spectrometer Mode の結果画面の題名は「スペクトル」)"""
    global _TITLES
    if _TITLES is None:
        folder = os.path.join(SCREENS, "titles")
        _TITLES = {f[:-4].replace("_", " "): Image.open(os.path.join(folder, f)).convert("RGB")
                   for f in sorted(os.listdir(folder)) if f.lower().endswith(".png")} if os.path.isdir(folder) else {}
    return _TITLES


def match_title(frame: Image.Image, samples: dict[str, Image.Image]) -> str:
    """題名の欄が見本のどのモードか(どれでもなければ "")"""
    crop = title_crop(frame)
    best = min(((_diff(crop, img), name) for name, img in samples.items()), default=(999.0, ""))
    return best[1] if best[0] <= DIFF_MAX else ""


_LIB: Library | None = None


def library() -> Library:
    global _LIB
    if _LIB is None:
        _LIB = Library()
    return _LIB
