"""配布用の exe を作る(PyInstaller、フォルダ形式)。開発用 PC で TRACERexl から実行する

    python -m pip install pyinstaller      (初回だけ。ネット接続が必要)
    python tests/build_exe.py              → dist/Traer5iAuto/Traer5iAuto.exe

できあがり(dist/Traer5iAuto/ をフォルダごと配る。zip にしてよい):
    Traer5iAuto.exe    ダブルクリックで起動(黒い窓は出ない)
    _internal/         Python 本体・Pillow・pywebview(WebView2)・画面(web)・照合の見本(screens)
    config/ と TracerData/ は初回起動で exe の隣に作られる(storage.base_dir)

方針:
  - 1ファイル形式(--onefile)にしない: 起動のたびに一時フォルダへ展開するので遅く、exe の隣に config を置けない。
  - OCR(winrt)は入れない。アプリは画面を見本との照合で読む(screen_match)。
  - 照合の見本(src/screens/)をデータとして含める(screen_match は自分の置き場所の隣の screens/ を読む)。
  - 開発用 PC にある numpy / OpenCV は使っていないので外す(紛れ込むと数十 MB 増える)。Tk も使わない(2026-11-20 に WebView2 版へ切り替えた)。
  - 画面(src/web/ の HTML・CSS・JavaScript)をデータとして含める。配布先に WebView2 Runtime が要る(Windows 11 は標準)。
  - exe に --selftest を付けると、機器に触れずに起動を確かめ、config/web_selftest.txt に結果を書いて閉じる(別の PC の確認用)。
  - 作業用の build/ と、できあがりの dist/ は TRACERexl 直下に作る(配布物の Traer5iAuto/ には何も足さない)。
"""
from __future__ import annotations

import os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))    # TRACERexl
SRC = os.path.join(ROOT, "Traer5iAuto", "src")
NAME = "Traer5iAuto"
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build")
EXCLUDE = ["numpy", "cv2", "winrt", "tkinter"]   # アプリでは使わないもの(winrt = OCR は見本を作るときと予備の --remotectrl だけ)


def pyinstaller_args() -> list[str]:
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noconsole", "--onedir",
            "--name", NAME, "--paths", SRC,
            "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
            "--add-data", f"{os.path.join(SRC, 'screens')}{os.pathsep}screens",   # 照合の見本(screen_match)
            "--add-data", f"{os.path.join(SRC, 'web')}{os.pathsep}web"]           # 画面(HTML・CSS・JavaScript)
    for m in EXCLUDE:
        args += ["--exclude-module", m]
    return args + [os.path.join(SRC, "web_app.py")]


def folder_mb(path: str) -> float:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs) / 1e6


def main() -> int:
    import importlib.util
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller が入っていません。先に次を実行してください(ネット接続が必要):\n"
              "    python -m pip install pyinstaller")
        return 1
    out = os.path.join(DIST, NAME)
    if os.path.isdir(out):
        shutil.rmtree(out)                 # 前回の exe を消してから作る(古いファイルが残らないように)
    print("作成中:", " ".join(pyinstaller_args()[3:]))
    if subprocess.run(pyinstaller_args(), cwd=ROOT).returncode != 0:
        print("失敗しました(上のメッセージを確認してください)")
        return 1
    exe = os.path.join(out, NAME + ".exe")
    if not os.path.exists(exe):
        print("exe ができていません:", exe)
        return 1
    leaked = [m for m in EXCLUDE if os.path.isdir(os.path.join(out, "_internal", m))]
    print(f"できました: {exe}\n  フォルダの大きさ: {folder_mb(out):.0f} MB")
    if leaked:
        print("  注意: 外したはずのモジュールが入っています:", ", ".join(leaked))
    print(f"  配布: dist/{NAME}/ をフォルダごと(zip にしてよい)。Python の無い PC で一度起動して確かめる"
          + "(exe に --selftest を付けると、起動を確かめて config/web_selftest.txt に結果を書いて閉じる)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
