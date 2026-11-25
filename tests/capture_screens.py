"""【臨時・開発用】アプリをふつうに起動し、使っている間に機器から届いた「見本で見たことのない画面」を記録する(2026-11-13)。

  使い方(アプリは閉じておく。TRACERexl で): python tests/capture_screens.py
    → いつものアプリの窓が出るので、ふだんどおり「接続」して測る。窓を閉じると記録も終わる。

  - 機器は同時に1つの相手としかつながらないので、別のつなぎ方はせず、アプリの接続(dl.LINK)に届いた画面を横から見るだけ。
    機器へは何も送らない(クリック・トリガーはアプリの操作だけ)。
  - 画面が落ち着いたら(STABLE_S のあいだ更新が無い)見本と同じ方法で読み、「種類」(状態・ボタンの名前と位置・題名・読めたか)が
    見本の画面のどれとも、記録済みのどれとも違えば Traer5iAuto/config/capture_new/ に PNG を1枚残す(最大 MAX_FILES 枚)。
    一覧は capture_new/capture_log.csv。
  - 見本に足し終わったら capture_new/ は消す(画像を溜めない)。アプリ本体には何も足していない。
"""
import csv, os, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "Traer5iAuto")
SRC = os.path.join(APP_DIR, "src")
sys.path.insert(0, SRC)
os.chdir(APP_DIR)                     # VBS と同じ基準(config / TracerData)

import auto_measure as am
import device_link as dl
import screen_match as sm

OUT = os.path.join(APP_DIR, "config", "capture_new")
STABLE_S = 0.4        # 更新がこの秒数止まったら、描き終わったとみなして読む
POLL_S = 0.15
MAX_FILES = 80
GRID = 8              # ボタンの位置をこの px 単位で丸めて種類に入れる(フォーカスの枠のずれを吸収)


def kind(frame) -> tuple:
    """画面の種類: (状態, 読めたか, 題名のモード, ボタンの(名前, 丸めた位置・大きさ) の並び)"""
    ui = sm.library().read(frame)
    buttons = tuple(sorted((b.text, *(round(v / 2 / GRID) for v in b.rect)) for b in ui.buttons))
    title = sm.match_title(frame, sm.title_samples())
    if not title:                     # 見本に無い題名: 題名の欄の形で区別する(モードごとの結果画面を取りこぼさない)
        t = sm.title_crop(frame).convert("L").resize((24, 4)).point(lambda v: 255 if v > 128 else 0)
        title = "?" + t.tobytes().hex()[:24]
    return am.screen_state(ui), sm.recognized(ui), title, buttons


def known_kinds() -> set:
    """見本の画面の種類(これと同じなら記録しない)"""
    out = set()
    for f in os.listdir(sm.SCREENS):
        if f.endswith(".png") and f != "keypad_digits.png":
            try:
                from PIL import Image
                out.add(kind(Image.open(os.path.join(sm.SCREENS, f)).convert("RGB")))
            except Exception:
                pass
    return out


def watch(stop: threading.Event, out: str = OUT, log=print):
    """(裏)アプリの接続に届く画面を見て、見たことのない種類を残す"""
    os.makedirs(out, exist_ok=True)
    seen = known_kinds()
    saved = len([f for f in os.listdir(out) if f.endswith(".png")])
    log_path = os.path.join(out, "capture_log.csv")
    last_seq, changed_at, done_seq = -1, time.time(), -1
    while not stop.is_set() and saved < MAX_FILES:
        time.sleep(POLL_S)
        link = dl.LINK
        if link is None or not link.connected or not link.seq:
            continue
        if link.seq != last_seq:
            last_seq, changed_at = link.seq, time.time()
            continue
        if last_seq == done_seq or time.time() - changed_at < STABLE_S:
            continue
        done_seq = last_seq
        try:
            frame = link.snapshot()
            k = kind(frame)
        except Exception as e:
            log(f"[記録] 読めませんでした: {e}")
            continue
        if k in seen:
            continue
        seen.add(k)
        saved += 1
        name = f"{saved:02d}_{k[0]}{'' if k[1] else '_unread'}_{time.strftime('%H%M%S')}.png"
        frame.save(os.path.join(out, name))
        new = not os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["file", "time", "state", "recognized", "title", "buttons"])
            w.writerow([name, time.strftime("%Y-%m-%d %H:%M:%S"), k[0], k[1], k[2],
                        " / ".join(b[0] or "(名前なし)" for b in k[3])])
        log(f"[記録] 新しい画面 {name}(状態 {k[0]}・題名 {k[2]})")


def main():
    import storage, web_app
    stop = threading.Event()
    th = threading.Thread(target=watch, args=(stop,), kwargs={"log": lambda m: storage.append_log(m)}, daemon=True)
    th.start()
    storage.append_log("---- 起動(画面の記録つき: tests/capture_screens.py)")
    try:
        web_app.main()                                 # いつものアプリ(WebView2 版)。閉じるまで戻らない
    finally:
        stop.set()
        dl.close_link()
        storage.append_log("---- 終了")
        n = len([f for f in os.listdir(OUT) if f.endswith(".png")]) if os.path.isdir(OUT) else 0
        print(f"記録した画面: {n} 枚({OUT})")


if __name__ == "__main__":
    main()
