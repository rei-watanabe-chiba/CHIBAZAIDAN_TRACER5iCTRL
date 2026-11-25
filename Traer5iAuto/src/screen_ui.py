"""読んだ機器の画面の形(ScreenUI / UIElement)・名前の照合・青いボタンの検出(画面を持たない。入力は送らない)

screen_match が見本との照合で ScreenUI を作り、auto_measure がそれで画面を判定・クリックする。座標は基準の 960 幅
(REF_W。機器の画面 480x640 を縦横 2 倍にした大きさ)。画像処理は Pillow だけ(配布を軽くするため)。
2026-11-25 に、RemoteCTRL の窓を相手にする予備の処理(bruker_ui.py)から、共通で使う部分だけをここへ移した。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from PIL import Image, ImageChops, ImageFilter

REF_W = 960          # 解析の基準幅(機器の画面 480px を 2 倍にした幅)


# ---------------------------------------------------------------- データ構造
@dataclass
class UIElement:
    kind: str                      # "button" / "text" など
    text: str
    rect: tuple[int, int, int, int]  # 基準座標 (x, y, w, h)

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.rect
        return x + w // 2, y + h // 2


@dataclass
class ScreenUI:
    image: Image.Image = field(repr=False)
    buttons: list[UIElement]
    texts: list[UIElement]
    scale: float                   # 機器の画面の幅(画像の px) / 基準幅(REF_W)。座標は全て基準座標系

    def find_any(self, label: str, min_score: float = 0.6) -> UIElement | None:
        """ボタン・テキストの両方から探す(リスト項目の選択などに使う)"""
        scored = [(_match_score(label, e.text), e) for e in self.buttons + self.texts]
        scored = [s for s in scored if s[0] >= min_score]
        return max(scored, key=lambda s: s[0])[1] if scored else None

    def find_button(self, label: str, min_score: float = 0.5) -> UIElement | None:
        """ラベルでボタンを探す。完全一致/部分一致を優先し、なければ類似度で探す"""
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


# ---------------------------------------------------------------- 名前の照合
_CONFUSABLE = str.maketrans({"0": "o", "1": "l", "|": "l", "i": "l", "5": "s"})


def _norm(s: str) -> str:
    """照合用の正規化: 空白除去・小文字化・取り違えやすい文字の統一 (0/O, 1/l/i, 5/S)"""
    return fix_katakana(s).replace(" ", "").replace("　", "").lower().translate(_CONFUSABLE)


def same_name(a: str, b: str) -> bool:
    """名前の一致判定(b に a が含まれるか)。完全な部分一致がなければ、b 内の同じ長さの区間と比べて
    8 割以上一致すれば同一とみなす"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb:
        return True
    if any(c.isdigit() for c in a) or len(na) < 6:
        return False      # 数字入り(例: Precious Metals 1/2)や短い名前は誤一致が危険なので厳密一致のみ
    L = len(na)
    best = max(SequenceMatcher(None, na, nb[i:i + L]).ratio() for i in range(max(1, len(nb) - L + 1)))
    return best >= 0.8


def _match_score(label: str, text: str) -> float:
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


def fix_katakana(s: str) -> str:
    """カタカナの後のハイフン類を長音記号に統一する"""
    out = []
    for i, c in enumerate(s):
        if c in "-‐－―─" and i > 0 and "゠" <= s[i - 1] <= "ヿ":
            c = "ー"
        out.append(c)
    return "".join(out)


# ---------------------------------------------------------------- 青いボタンの検出
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


def detect_buttons(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """青色(機器の画面のボタンの色)の矩形を見つける"""
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
