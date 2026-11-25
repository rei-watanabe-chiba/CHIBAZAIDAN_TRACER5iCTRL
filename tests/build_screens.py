"""照合の見本(Traer5iAuto/src/screens/)を作る(開発用。OCR を使うのはここだけ)

    python tests/build_screens.py <機器の画面の PNG を集めたフォルダ>

集めた画面(機器の 480x640)を OCR で一度だけ読み、ボタン・文字の位置と名前を screens.json にする。
OCR の読み違いは FIX で直す。上の状態の行(y < 25)・数字だけの文字(値)・読めない文字は入れない。
数字の字形は、テンキーの表示欄に 1234567890 を打った画面(keypad_digits)から取る。
"""
from __future__ import annotations

import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "Traer5iAuto", "src")
sys.path.insert(0, SRC)
from PIL import Image

import bruker_ui as bu
import screen_match as sm

OUT = os.path.join(SRC, "screens")
FIX = {                                  # OCR の読み違い → 正しい名前
    "設": "設定", "0 K": "OK", "D E L": "DEL", "テフォルト設定を使用": "デフォルト設定を使用",
    "Spectrometer MOde": "Spectrometer Mode", "手動 トリガアクテイプ": "手動 トリガアクティブ",
    "自動 トリガアクテイプ": "自動 トリガアクティブ", "ログインD:": "ログインID:", "1時間": "1 時間",
    "・本計器は電離放射線を発生します。": "警告: 本計器は電離放射線を発生します。", "アプリケー": "アプリケーション設定",
    "コリメー": "コリメータ", "方法 AutO": "方法 Auto", '"初期化-準備完了"': '"初期化 - 準備完了"',
}
DROP = re.compile(r"[\[\]〔〕]|^B R K E R$|^ミ/|ージョン:|^囘|^\d+%$|^谷")   # 読めていない文字(起動画面のバージョン・初期化中の % は変わるので入れない。放射線マークの読み違い)
STATUS_H = 25                                         # 上の状態の行(時刻・電池などで変わる)
NUMBER_BOXES = {"settings_Spectrometer_Mode": [("", (156, 441, 52, 31))]}   # (前に付ける文字, 欄) 時間の欄(「期間」の右)
# 測定中・結果の画面: 上から「ファイル No 日付 時刻」の行と「時間 15.0」の行(値は毎回変わるので、文字ではなく数字として読む)
RESULT_LINES = [("", (3, 80, 297, 24)), ("時間", (55, 104, 245, 36))]
DYNAMIC = ("measuring", "result", "acquiring")        # 値が毎回変わる画面: 文字は見本にせず、ボタンと数字の行だけ
EXTRA_GLYPHS = [("-", "result_Obsidian", (86, 94, 92, 96)), (":", "result_Obsidian", (154, 89, 156, 101)),
                (".", "result_Obsidian", (89, 131, 91, 133))]   # 結果の画面の記号の字形(No.756 の画面から)


def main(folder: str) -> int:
    rc = object.__new__(bu.RemoteCtrl); rc.ocr = bu._Ocr("ja")
    screens, items = [], []
    os.makedirs(OUT, exist_ok=True)
    for name in sorted(os.path.splitext(f)[0] for f in os.listdir(folder) if f.endswith(".png")):
        if name.startswith("error"):
            continue
        img = Image.open(os.path.join(folder, name + ".png")).convert("RGB")
        if img.size != (sm.W, sm.H):
            print("大きさが違うので使わない:", name, img.size); continue
        img.save(os.path.join(OUT, name + ".png"), optimize=True)
        screens.append(name)
        ui = rc.read_ui(img.resize((sm.W * sm.K, sm.H * sm.K), Image.NEAREST), content_w=sm.W * sm.K)
        if name == "login":                               # テンキーの数字は OCR で読めないので、位置で名前を付ける
            keys = sorted((b for b in ui.buttons if not b.text), key=lambda b: (b.rect[1] // 40, b.rect[0]))
            for b, ch in zip(keys, "1234567890"):
                b.text = ch
        for kind, elems in (("button", ui.buttons), ("text", ui.texts)):
            for e in elems:
                r = [v // sm.K for v in e.rect]
                label = FIX.get(e.text, e.text).replace("ハックスペース", "バックスペース").replace("0 K", "OK")
                if kind == "text" and name.startswith(DYNAMIC):
                    continue
                if kind == "text" and (r[1] < STATUS_H or DROP.search(label) or not label.strip()
                                       or re.fullmatch(r"[\d .,]+", label) or label.startswith(("kV", "uA"))):
                    continue
                items.append({"kind": kind, "label": label, "rect": r, "screen": name})
        if name == "confirm":                             # 「現在の構成:」の行(アプリによらず同じ。下の行はアプリ名で変わる)
            blob = max(ui.buttons, key=lambda b: b.rect[2] * b.rect[3])
            x, y, w, h = blob.rect
            line = rc.ocr.lines(ui.image.crop((x, y, x + w, y + h)))[0]
            lx, ly, lw, lh = line[1]
            items.append({"kind": "text", "label": "現在の構成:", "screen": name,
                          "rect": [(x + lx) // sm.K - 2, (y + ly) // sm.K - 2, lw // sm.K + 4, lh // sm.K + 4]})
        lines = NUMBER_BOXES.get(name, []) + (RESULT_LINES if name.startswith(("measuring", "result")) else [])
        for prefix, r in lines:
            items.append({"kind": "number", "label": prefix, "rect": list(r), "screen": name})
    # 数字の字形(テンキーの表示欄の 1234567890)
    img = Image.open(os.path.join(OUT, "keypad_digits.png")).convert("RGB")
    area = (18, 132, 250, 32)
    boxes = sm._glyph_boxes(img.crop((area[0], area[1], area[0] + area[2], area[1] + area[3])))
    chars = "1234567890"
    if len(boxes) != len(chars):
        print("数字の字形を", len(chars), "個に分けられません:", boxes); return 1
    digits = {"screen": "keypad_digits", "chars": chars,
              "boxes": [[area[0] + b[0], area[1] + b[1], area[0] + b[2], area[1] + b[3]] for b in boxes]}
    with open(os.path.join(OUT, "screens.json"), "w", encoding="utf-8") as f:
        glyphs = [{"char": c, "screen": n, "box": list(b)} for c, n, b in EXTRA_GLYPHS if n in screens]
        json.dump({"screens": screens, "items": items, "digits": digits, "glyphs": glyphs}, f, ensure_ascii=False, indent=1)
    print(f"見本: 画面 {len(screens)} / ボタン {sum(i['kind'] == 'button' for i in items)} / 文字 {sum(i['kind'] == 'text' for i in items)}"
          f" / 数字の欄 {sum(i['kind'] == 'number' for i in items)} / 字形 {len(boxes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
