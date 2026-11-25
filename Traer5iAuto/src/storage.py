"""保存まわりの共通処理: フォルダの置き場所・安全な書き込み・設定の読み書き

フォルダはすべて「基準のフォルダ」からの相対パス:
    start_measure_gui.vbs / Traer5iAuto.exe  ← 基準(作業フォルダ)
    config/                設定・実行記録・異常時の画面
    TracerData/            保存先(固定)
基準のフォルダは、exe で起動したときは exe のあるフォルダ、Python で起動したときは VBS のあるフォルダ(src の1つ上)。
VBS は作業フォルダを自分のフォルダにしてから起動する。それ以外(exe・src で直接実行など)は、
起動時に use_base() で作業フォルダを基準のフォルダへ移してから相対パスを使う。
"""
from __future__ import annotations

import json, os, sys, threading, time

# ---------------------------------------------------------------- フォルダ
VBS = "start_measure_gui.vbs"
CONFIG_DIR = "config"
TRACER_DIR = "TracerData"
SETTINGS = os.path.join(CONFIG_DIR, "gui_settings.json")
LOG_CSV = os.path.join(CONFIG_DIR, "measure_log.csv")
LEDGER_STORE = os.path.join(CONFIG_DIR, "ledger.json")  # 台帳の正本(全部の行とターンの記録。保存先の File.csv は有効な行だけ)
ERROR_PNG = os.path.join(CONFIG_DIR, "last_error.png")
DIAG_DIR = os.path.join(CONFIG_DIR, "remotectrl_test")     # bruker_ui の診断画像
APP_LOG = os.path.join(CONFIG_DIR, "app_log.txt")          # アプリのログ・アラート・思わぬエラー(画面を閉じても残る)
APP_LOG_MAX = 300_000                                      # これを超えたら古い半分を消す(バイト)
NEW_SCREENS_DIR = os.path.join(CONFIG_DIR, "new_screens")  # 照合の見本に無かった機器の画面(見本を足すため。最大 NEW_SCREENS_MAX 枚)

_LAYOUT_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # src の1つ上(VBS の置き場所)


def base_dir() -> str:
    """基準のフォルダ: exe(PyInstaller)なら exe のあるフォルダ、Python なら VBS のあるフォルダ"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _LAYOUT_BASE


def use_base():
    """作業フォルダを基準のフォルダにする(起動時に1回だけ呼ぶ)。
    Python では、すでに VBS のフォルダにいればそのまま(VBS が作業フォルダを決めている)"""
    if getattr(sys, "frozen", False) or not os.path.exists(VBS):
        os.chdir(base_dir())


# ---------------------------------------------------------------- 書き込み
def ensure_parent(path: str):
    """path を置くフォルダが無ければ作る(初回起動で config / TracerData が無いとき用)"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def write_atomic(path: str, data: bytes | str, encoding: str = "utf-8"):
    """一時ファイルに書いてから置き換える(書き込み中に落ちても元のファイルは壊れない。既存は上書き)。
    置き換えは、ほかの糸・ソフトが一瞬読んでいて断られたら少し待って試し直す(REPLACE_TRIES 回)。
    失敗したら OSError(Excel で開いている など)"""
    ensure_parent(path)
    tmp = f"{path}.{threading.get_ident()}.tmp"    # 糸ごとに別の一時ファイル(同じ名前だと、同時に書いた糸の置き換えが失敗する)
    if isinstance(data, str):
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    for n in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if n == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT_S)


REPLACE_TRIES = 5          # write_atomic の置き換えを試す回数(読んでいる最中だと Windows は置き換えを断る)
REPLACE_WAIT_S = 0.05


NL = chr(10)


def append_log(line: str, path: str = APP_LOG):
    """ログのファイルに1行足す(時刻付き)。大きくなったら古い半分を消す。失敗しても止めない"""
    import time
    try:
        ensure_parent(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}" + NL)
        if os.path.getsize(path) > APP_LOG_MAX:
            with open(path, encoding="utf-8") as f:
                keep = f.read()[-APP_LOG_MAX // 2:]
            write_atomic(path, keep[keep.find(NL) + 1:])
    except OSError:
        pass


# ---------------------------------------------------------------- 設定(config/gui_settings.json)
def load_settings(path: str = SETTINGS) -> dict:
    """設定を読む。無い・壊れているときは空"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict, path: str = SETTINGS):
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))


# ---- アプリは 1 つだけ(2026-11-20: 2 つ起動して 2 つ目も機器につなぎに行った後、トリガーがロックした。原因かは未確認)
INSTANCE_MUTEX = r"Local\Traer5iAuto"    # アプリで 1 つ(2 つ目は機器につながずに終わる)
_mutex = None


def single_instance(name: str = INSTANCE_MUTEX) -> bool:
    """このユーザーの中でアプリが 1 つ目なら True(印の Mutex を持ち続ける)。もう動いていれば False。
    確かめられないとき(Windows 以外など)は True"""
    global _mutex
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.CreateMutexW(None, False, name)
        if not h:
            return True
        if ctypes.get_last_error() == 183:     # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _mutex = h                             # プロセスが終わるまで持つ(終われば Windows が片付ける)
        return True
    except Exception:
        return True


def show_running(title: str) -> bool:
    """もう動いているアプリの窓(題名 title)を最大化して前に出す。知らせの窓は出さない(2026-11-20 ユーザーの判断)。
    見つかれば True"""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, title)
        if not hwnd:
            return False
        u32.ShowWindow(hwnd, 3)                # SW_MAXIMIZE(最小化していても最大化で出す)
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
