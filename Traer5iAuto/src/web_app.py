"""アプリの起動ファイル(画面は WebView2。2026-11-19 に作り始め、2026-11-20 に Tk 版から切り替えた)

    python web_app.py          (配布では start_measure_gui.vbs か Traer5iAuto.exe から起動する)

作り:
  - 窓は pywebview(Windows では WebView2)。画面は web/ の HTML・CSS・JavaScript。
  - Python → 画面: Bridge.emit(種類, 中身)。順番どおりに、少しずつまとめて送る(app.receive)。本体画面の絵のように
    最後の 1 つだけでよいものは replace=True(まだ送っていない古いものは捨てる)。
  - 画面 → Python: Api のメソッド(pywebview.api.…)。中身の処理は Bridge.run で 1 本の糸(画面の糸)に渡して順に行う
    (アプリの状態を変えるのはいつもこの糸だけ)。
  - 見た目の値は gui_config.UI をそのまま画面へ渡す(色は CSS の変数 --名前 になる)。見た目の変更は今までどおり gui_config だけ。
  - 閉じるとき: 照射中ならトリガーを離してから(shutdown.release_trigger)、設定を保存し、機器との接続を切る。
  - 中身は controller.Controller(測定の進め方・測定モード表・台帳・資料一覧)と device_core.DeviceWatch(本体画面)で、
    ここは画面へ知らせを送るだけ。照射は「自動計測」の後の確認で、安全確認がすべてチェックされたときだけ(Controller.confirm_answer)。
"""
from __future__ import annotations

import atexit, base64, ctypes, io, json, os, queue, sys, threading, time, traceback

import auto_measure as am
import device_link as dl
from app_rules import choose_zoom, mini_parts, place_in, status_look, toggle_mini
from gui_config import UI
from storage import append_log, load_settings, save_settings, show_running, single_instance
from ctypes import wintypes as wt

# 窓の知らせを先に受け取る関数の形(comctl32 の SUBCLASSPROC)
_SUBCLASSPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, ctypes.c_size_t, ctypes.c_size_t)


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT), ("ptMin", wt.POINT),
                ("ptMax", wt.POINT), ("rc", wt.RECT)]

FROZEN = getattr(sys, "frozen", False)
WEB_DIR = os.path.join(sys._MEIPASS if FROZEN else os.path.dirname(os.path.abspath(__file__)), "web")
SEND_BATCH_MS = 15          # 画面へ送る知らせを、この間に来た分までまとめて 1 回で送る
SEND_BATCH_MAX = 200        # 1 回にまとめる数の上限


def ui_values() -> dict:
    """画面へ渡す gui_config.UI の値(JSON にできるものだけ。タプルはリストになる)"""
    out = {}
    for k, v in vars(UI).items():
        if k.startswith("_") or callable(v) or isinstance(v, (staticmethod, classmethod)):
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


CACHE_DIRS = ("Cache", "Code Cache", "GPUCache")


def clear_cache(storage: str):
    """WebView2 の溜めた画面のファイル(js・css)を消す。消さないと改修した後も前の版の画面が動く(2026-11-20 実機)。
    設定(倍率・窓の位置など)は Python の側に保存しているので、ここを消しても失うものは無い"""
    import shutil
    for name in CACHE_DIRS:
        shutil.rmtree(os.path.join(storage, "EBWebView", "Default", name), ignore_errors=True)


def work_area_css() -> tuple[float, tuple[int, int, int, int]]:
    """(画面の倍率(DPI / 96), 主な画面の作業領域(CSS の px。タスクバーを除く))"""
    try:
        import ctypes.wintypes as wt
        k = ctypes.windll.user32.GetDpiForSystem() / 96
        r = wt.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA(物理 px)
        return k, (round(r.left / k), round(r.top / k), round(r.right / k), round(r.bottom / k))
    except (AttributeError, OSError):
        return 1.0, (0, 0, 1920, 1040)


def bring_front(win):
    """起動したとき窓を前に出す(Windows はほかの窓を使っている間は前に出させないことがあるので、いったん常に前面にして戻す)"""
    try:
        u32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(win.native.Handle.ToInt64()))
        flags = 0x0001 | 0x0002                      # NOSIZE・NOMOVE
        u32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)   # HWND_TOPMOST
        u32.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)   # HWND_NOTOPMOST(一番上のまま、常に前面は外す)
        u32.SetForegroundWindow(hwnd)
    except Exception:
        append_log("[起動した窓を前に出せない] " + traceback.format_exc(limit=2))


class Bridge:
    """Python と画面のやり取り。emit は どの糸から呼んでもよい。run に渡した処理は画面の糸で順に行う"""

    def __init__(self):
        self.window = None
        self.ready = threading.Event()               # 画面の JavaScript が受け取れるようになった(Api.ready)
        self.closed = threading.Event()
        self._out: queue.Queue = queue.Queue()
        self._latest: dict[str, object] = {}         # replace の知らせ: 種類 -> 最後の中身(まだ送っていない)
        self._lock = threading.Lock()
        self._tasks: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._work, name="画面の糸", daemon=True)
        self.thread.start()
        self.sender = threading.Thread(target=self._send, name="画面へ送る糸", daemon=True)
        self.sender.start()

    # ---- Python → 画面
    def emit(self, kind: str, data=None, replace: bool = False):
        if replace:
            with self._lock:
                fresh = kind not in self._latest
                self._latest[kind] = data
            if fresh:
                self._out.put((kind, None, True))
        else:
            self._out.put((kind, data, False))

    def _send(self):
        self.ready.wait()
        while not self.closed.is_set():
            try:
                batch = [self._out.get(timeout=0.5)]
            except queue.Empty:
                continue
            end = time.time() + SEND_BATCH_MS / 1000
            while len(batch) < SEND_BATCH_MAX:
                try:
                    batch.append(self._out.get(timeout=max(0.0, end - time.time())))
                except queue.Empty:
                    break
            msgs = []
            for kind, data, rep in batch:
                if rep:
                    with self._lock:
                        if kind not in self._latest:
                            continue
                        data = self._latest.pop(kind)
                msgs.append([kind, data])
            if self.closed.is_set():                 # 窓を閉じた後は送らない(閉じた窓へ送ると .NET のエラーになる)
                return
            try:
                self.window.evaluate_js(f"app.receive({json.dumps(msgs, ensure_ascii=False)})")
            except Exception:
                if self.closed.is_set():
                    return
                append_log("[画面へ送れない] " + traceback.format_exc(limit=2))

    def stop(self):
        """窓を閉じる: これより後は画面へ送らない(送っている途中なら終わるのを待つ)"""
        self.closed.set()
        if self.sender.is_alive() and threading.current_thread() is not self.sender:
            self.sender.join(2)

    # ---- 画面の糸
    def run(self, fn, *args, wait: bool = True, timeout: float | None = 30):
        """fn(*args) を画面の糸で行う。wait なら終わるまで待って戻り値を返す(例外はそのまま投げる)"""
        if threading.current_thread() is self.thread:
            return fn(*args)
        box: dict = {}
        done = threading.Event()
        self._tasks.put((fn, args, box, done))
        if not wait:
            return None
        if not done.wait(timeout):
            raise TimeoutError("画面の糸が答えません")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def after(self, ms: int, fn, *args):
        """ms 後に fn を画面の糸で行う"""
        t = threading.Timer(ms / 1000, lambda: self.run(fn, *args, wait=False))
        t.daemon = True
        t.start()
        return t

    def _work(self):
        while True:
            fn, args, box, done = self._tasks.get()
            try:
                box["value"] = fn(*args)
            except BaseException as e:               # noqa: BLE001(呼んだ側へ返す)
                box["error"] = e
                append_log("[画面の糸のエラー] " + "".join(traceback.format_exception(e)))
            done.set()


class Api:
    """画面から呼べるもの(pywebview.api.…)。名前が _ で始まるものは画面に見せない。中身は画面の糸で行う"""

    def __init__(self, app: "WebApp"):
        self._app = app

    def _do(self, fn, *args):
        return self._app.bridge.run(fn, *args)

    def hello(self):
        """画面ができた: 見た目の値と倍率を渡す"""
        return self._do(self._app.hello)

    def ready(self):
        """画面が知らせを受け取れるようになった"""
        self._app.bridge.ready.set()
        return True

    # ---- 小窓(最小化したときの同じ窓)
    def set_mini_top(self, on):
        """小窓の「常時前面に表示する」"""
        return self._app.set_mini_top(bool(on))

    def set_mini_enabled(self, on):
        """「最小化時に小窓にする」"""
        return self._app.set_mini_enabled(bool(on))

    def set_mini_view(self, part):
        """小窓のボタン: なし / モード / 画面(モード・画面はそれぞれ入り切りする)"""
        return self._app.set_mini_view(part)

    def fit_mini(self, height):
        """小窓の高さを中身に合わせる(height = 中身の高さ。物理の px)"""
        return self._app.fit_mini(float(height))

    def log(self, text):
        """画面のエラーなどをログのファイルへ"""
        append_log(f"[画面] {text}")
        return True

    # ---- 本体画面
    def click(self, x, y):
        """本体画面のクリック(機器の画面の px)。手で操作してよいときだけ機器へ送る"""
        return self._do(self._app.device.click, int(x), int(y))

    def select_device(self, key):
        return self._do(self._app.device.select, key)

    def connect(self):
        """「接続」: 一覧で選んだ機器につなぐ"""
        return self._do(self._app.device.connect_found)

    # ---- 状態と操作
    def main(self):
        """メインのボタン(自動計測 / 再計測 / 再開 / ログイン / 画面更新 / 停止)"""
        return self._do(self._app.ctl.main_clicked)

    def end_turn(self):
        """「終了」(再計測・再開の横)"""
        return self._do(self._app.ctl.end_turn)

    def confirm(self, ok, checks):
        """照射前の確認の答え(照射開始 / キャンセル と、安全確認のチェック)。照射を始めたら True"""
        return self._do(self._app.ctl.confirm_answer, ok is True, [c is True for c in (checks or [])])

    def set_pass(self, code):
        return self._do(self._app.ctl.set_pass, str(code))

    def set_skip_phases(self, skip):
        return self._do(self._app.ctl.set_skip_phases, bool(skip))

    def set_values(self, values):
        """計測ファイルの欄の値(3 段目で画面を作る。変えられない間は受け付けない)"""
        def job():
            if not self._app.ctl.ledger.locked:
                self._app.ctl.ledger.set_values({k: str(v) for k, v in dict(values).items()})
        return self._do(job)

    # ---- 資料一覧
    def refs_open(self):
        """「CSV選択」: ファイルを選ぶ窓(この糸で待つ。画面の糸はふさがない)→ 読む"""
        import webview
        start = os.path.dirname(self._app.ctl.refs.data.path or "") or ""
        try:
            got = self._app.window.create_file_dialog(webview.FileDialog.OPEN, directory=start,
                                                      file_types=("CSV (*.csv;*.txt)", "すべて (*.*)"))
        except Exception as e:
            append_log(f"[CSV選択] {e!r}")
            return False
        if not got:
            return False
        return self._do(self._refs_load, got[0])

    def refs_load(self, path):
        return self._do(self._refs_load, str(path))

    def _refs_load(self, path):
        ok = self._app.ctl.refs.load(path)
        self._app.ctl._save()
        return ok

    def refs_search(self, on, target, query):
        return self._do(self._app.ctl.refs.search, bool(on), str(target), str(query))

    def refs_sort(self, col):
        return self._do(self._app.ctl.refs.sort_by, str(col))

    def refs_assign(self, col, role):
        return self._do(self._app.ctl.refs.assign, str(col), role if role else None)

    def refs_join(self, a, sep, b):
        def job():
            ok = self._app.ctl.refs.make_join(str(a), str(sep), str(b))
            self._app.ctl._save()
            return ok
        return self._do(job)

    def refs_unjoin(self):
        def job():
            self._app.ctl.refs.drop_join()
            self._app.ctl._save()
        return self._do(job)

    def refs_use(self, n):
        return self._do(self._app.ctl.refs.use, int(n))

    # ---- 計測台帳
    def ledger_cell(self, rid, col, value):
        return self._do(self._app.ctl.ledger.set_cell, str(rid), str(col), str(value))

    def ledger_failed(self, ids, failed):
        """右クリックの「有効にする」(failed=False)/「失敗にする」(failed=True。理由は手動変更)"""
        return self._do(self._app.ctl.ledger.set_failed, [str(i) for i in ids or []], UI.LEDGER_FAILED_MANUAL if failed else "")

    def ledger_delete(self, ids):
        """「台帳から削除する」(画面の側で確かめてから)"""
        return self._do(self._app.ctl.ledger.delete, [str(i) for i in ids or []])

    def open_folder(self):
        """「保存先」: 保存先のフォルダを開く(ファイルは開かない)"""
        os.startfile(os.path.abspath(self._app.ctl.ledger.folder))
        return True

    def save_layout(self, layout):
        """段の境目の位置・台帳と資料一覧の列の幅を設定に保存する"""
        keep = {k: v for k, v in dict(layout or {}).items() if k in ("right_split", "ledger_col_widths", "refs_col_widths")}
        return self._do(self._app._save_settings, keep)

    # ---- 測定モード表
    def mode_add(self, after=None):
        return self._do(self._app.ctl.modes.add, after)

    def mode_delete(self, ids):
        return self._do(self._app.ctl.modes.delete, list(ids or []))

    def mode_set(self, rid, app):
        return self._do(self._app.ctl.modes.set_mode, rid, str(app))

    def mode_phases(self, rid, values, commit=True):
        return self._do(self._app.ctl.modes.set_phases, rid, [str(v) for v in values], bool(commit))

    def mode_revert(self, rid):
        return self._do(self._app.ctl.modes.revert, rid)


class WebApp:
    """WebView2 版のアプリ: 窓・やり取り・左の列(状態の欄・ボタン・測定モード表・設定・本体画面)。右の列の中身は 3 段目。
    画面の側の役目(controller.Controller の view・device_core.DeviceWatch の view)は、知らせを画面へ送るだけ"""

    def __init__(self, settings_path: str | None = None):
        import controller, device_core
        self.settings_path = settings_path or am.SETTINGS
        self.settings = load_settings(self.settings_path)
        self.bridge = Bridge()
        self.api = Api(self)
        self.maximized = bool(self.settings.get("web_zoomed"))
        self.minimized = False
        self.window = None
        self.compact = False                         # 小窓(最小化したときに同じ窓を小さくした形)の間
        self._normal = None                          # 小窓にする前の窓: (x, y, 幅, 高さ, 最大化だったか)。物理の px
        self._min_size = None                        # 小窓の間は窓の最小の大きさを外す(戻すときに使う)
        self._mini_wh = None                         # 小窓の大きさ(物理の px)。外から変えられたら戻す(_keep_mini)
        self._mini_xy = None                         # 小窓の位置(物理の px。_keep_mini_pos で覚える)
        self._keep_hooked = False
        self._win_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._quitting = False
        self.dpi_k, self.area = work_area_css()
        self.zoom = choose_zoom(self.area[3] - self.area[1], 1.0, self.settings.get("zoom"))
        dl.IN_USE = False                            # 「接続」するまで、測定の再開はつながったとみなさない
        dl.NOTE = am.TRACE = append_log              # 時間のかかり方はログのファイルにだけ
        after = lambda ms, fn, *a: self.bridge.after(ms, fn, *a)
        post = lambda fn, *a: self.bridge.run(fn, *a, wait=False)
        ctl = lambda name: (lambda *a: getattr(self.ctl, name)(*a))   # 呼び返し(ctl を作る前に渡す)
        self.device = device_core.DeviceWatch(self, after, alert=ctl("_alert"), on_busy=ctl("device_busy"),
                                              on_connected=ctl("device_connected"), on_lost=ctl("device_lost"),
                                              on_trigger=ctl("trigger_changed"), on_booted=ctl("device_booted"),
                                              note=append_log)
        self.ctl = controller.Controller(self, after, post, self.settings, self._save_settings, self.device)
        self.bridge.run(self._begin)

    def _begin(self):
        self.ctl.start(self.settings)
        self.emit("settings", {"pass": self.ctl.pass_code, "skip": not self.ctl.set_each, "mini": self.mini_enabled(),
                               "top": self.mini_on_top()})
        self.emit("mini_view", mini_parts(self.settings.get("mini_view")), replace=True)
        self.device.start()

    # ---- 窓
    def create_window(self):
        import webview
        saved = self.settings.get("web_window")
        min_w, min_h = (round(v * self.zoom) for v in UI.WEB_MIN_SIZE)
        w, h, x, y = place_in(self.area, saved if isinstance(saved, list) and len(saved) == 4 else None,
                              (min_w, min_h), UI.WIN_FRACTION)
        self.window = webview.create_window(UI.WEB_TITLE, os.path.join(WEB_DIR, "index.html"), js_api=self.api,
                                            width=w, height=h, x=x, y=y, min_size=(min_w, min_h),
                                            background_color=UI.WEB_BG, maximized=self.maximized)
        self.bridge.window = self.window
        ev = self.window.events
        ev.closing += self._closing
        ev.closed += self._closed
        later = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()   # 窓の知らせの中で窓を操作しない
        ev.shown += lambda: later(bring_front, self.window)
        ev.shown += lambda: later(self._form, self._hook_syscmd)
        ev.minimized += lambda: later(self._minimized)
        ev.maximized += lambda: later(self._maximized)
        ev.restored += lambda: later(self._restored)
        return self.window

    # ---- 小窓(2026-11-20 ユーザーの判断): 最小化すると、同じ窓を状態の欄とボタンだけの小さな窓にする(大きさは変えられない)。
    # 小窓で最小化を押すと、常時前面に表示するがオフなら最小化・オンなら小窓のまま。小窓で最大化を押すと元の窓に戻す。
    # 小窓の位置は設定 mini_pos に残す(無ければ作業領域の左上)
    def mini_enabled(self) -> bool:
        return bool(self.settings.get("mini_enabled", True))   # 起動時に読んだ設定(変えたらここも変える)

    def mini_on_top(self) -> bool:
        return bool(self.settings.get("mini_on_top", True))

    def set_mini_enabled(self, on: bool):
        self.settings["mini_enabled"] = on
        self._save_settings({"mini_enabled": on})
        return True

    def set_mini_top(self, on: bool):
        self.settings["mini_on_top"] = on
        self._save_settings({"mini_on_top": on})
        if self.compact:
            self._form(lambda f: self._apply_top(f, on))
        return True

    @staticmethod
    def _apply_top(form, on: bool):
        """常時前面にする / やめる。常時前面の間は最小化のボタンを押せなくする(Win + D など Windows 側の最小化は _minimized で戻す)"""
        form.TopMost = on
        form.MinimizeBox = not on

    def set_mini_view(self, part):
        if part != UI.MINI_NONE and part not in UI.MINI_VIEWS:
            return False
        parts = toggle_mini(self.settings.get("mini_view"), part)
        self.settings["mini_view"] = parts
        self._save_settings({"mini_view": parts})
        self.emit("mini_view", parts, replace=True)
        return True

    def fit_mini(self, height: float):
        """小窓の高さを中身に合わせる。作業領域に収まらなければ上へずらし、それでも入らなければ作業領域の高さまで(中はスクロール)"""
        if not 0 < height < 20000:
            return False
        import System.Windows.Forms as WF

        def go(f):
            if f.WindowState != WF.FormWindowState.Normal:
                return
            a = WF.Screen.FromControl(f).WorkingArea
            h = min(round(height) + f.Height - f.ClientSize.Height, a.Height)
            self._mini_wh = (f.Width, h)
            f.SetBounds(f.Left, max(a.Top, min(f.Top, a.Bottom - h)), f.Width, h)
        with self._win_lock:
            if self.compact:
                self._form(go)
        return True

    def _form(self, fn):
        """主の窓(WinForms の Form)の糸で fn(form) を行い、終わるまで待つ"""
        from System import Action
        form = self.window.native
        if form.InvokeRequired:
            form.Invoke(Action(lambda: fn(form)))
        else:
            fn(form)

    def _minimized(self):
        with self._win_lock:
            if self._quitting:
                return
            if self.compact:
                if self.mini_on_top():               # 常時前面なら最小化しない(小窓のまま)
                    self._form(lambda f: setattr(f, "WindowState", f.WindowState.Normal))
                else:
                    self.minimized = True
                return
            self.minimized = True
            if self.mini_enabled():
                self._enter_compact()

    def _maximized(self):
        with self._win_lock:
            if self.compact and self._state() == "max":   # 小窓の最大化: 元の窓に戻す
                self._leave_compact()
            elif not self.compact:
                self._sync()

    def _restored(self):
        with self._win_lock:
            if self.compact:
                self.minimized = False               # 小窓のまま(最小化から戻った)
            else:
                self._sync()

    def _state(self) -> str:
        """今の窓の状態: max / min / normal(知らせは別の糸で順番が入れ替わりうるので、窓そのものを見る)"""
        box = {}
        self._form(lambda f: box.setdefault("s", str(f.WindowState)))
        return {"Maximized": "max", "Minimized": "min"}.get(box["s"], "normal")

    def _sync(self):
        st = self._state()
        if st != "min":
            self.maximized, self.minimized = st == "max", False

    def _mini_rect(self) -> tuple[int, int, int, int]:
        """小窓の位置と大きさ(物理の px)。位置は保存したもの(どの画面にも入らなければ作業領域の左上)"""
        import System.Windows.Forms as WF
        k = self.dpi_k * self.zoom
        w, h = round(UI.MINI_SIZE[0] * k), round(UI.MINI_SIZE[1] * k)
        pos = self.settings.get("mini_pos")
        if isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, int) for v in pos):
            for sc in WF.Screen.AllScreens:
                a = sc.WorkingArea
                if a.Left <= pos[0] <= a.Right - 40 and a.Top <= pos[1] <= a.Bottom - 40:
                    return pos[0], pos[1], w, h
        a = WF.Screen.PrimaryScreen.WorkingArea
        return a.Left, a.Top, w, h

    def _enter_compact(self):
        import System.Drawing as D
        import System.Windows.Forms as WF
        x, y, w, h = self._mini_rect()
        if self._mini_wh and self._mini_wh[0] == w:
            h = self._mini_wh[1]                     # 前の小窓で中身に合わせた高さ(出た直後に高さが跳ねないように)
        was_max = self.maximized

        def go(f):
            self._animate(f, False)
            self._normal = (*self._normal_rect(f), was_max)
            self._min_size = f.MinimumSize
            f.MinimumSize = D.Size(0, 0)
            self._mini_wh, self._mini_xy = (w, h), (x, y)
            if not self._keep_hooked:
                f.SizeChanged += self._keep_mini
                f.LocationChanged += self._keep_mini_pos
                self._keep_hooked = True
            if f.WindowState != WF.FormWindowState.Normal:
                f.Bounds = D.Rectangle(x, y, w, h)                  # WinForms が覚える「元に戻す大きさ」も小窓に(覚えた大きさを一度経由しないように)
            self._place(f, False, x, y, w, h)                       # 途中の大きさを挟まずに小窓へ(枠を変えるより先に: 最大化のまま枠を変えると一度元の大きさに戻る)
            f.FormBorderStyle = WF.FormBorderStyle.FixedSingle      # 大きさを変えられない(最小化・最大化のボタンは残す)
            f.MaximizeBox = True
            self._apply_top(f, self.mini_on_top())
            f.Bounds = D.Rectangle(x, y, w, h)
            self._animate(f, True)
        self.compact = True
        self._form(go)
        self.minimized = False
        self.emit("compact", True, replace=True)

    def _hook_syscmd(self, form):
        """タイトルバーの最小化・最大化(ボタン・ダブルクリック・窓のメニュー)を、Windows が窓を動かす前に受け取る(窓の糸で)。
        通常の窓の最小化 → 最小化せずに小窓へ、小窓の最大化 → 最大化せずに元の窓へ(2026-11-24: 一度最小化・最大化してから
        戻していたので、縮む・広がるアニメーションが続けて出てちらついた)。Win + D など Windows 側の最小化は _minimized で戻す"""
        cc = ctypes.windll.comctl32
        cc.DefSubclassProc.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        cc.DefSubclassProc.restype = ctypes.c_ssize_t
        later = lambda fn: threading.Thread(target=fn, daemon=True).start()

        def proc(hwnd, msg, wp, lp, _id, _data):
            try:
                if msg == 0x0082:                                       # WM_NCDESTROY: 窓が消える。受け取りをやめる
                    cc.RemoveWindowSubclass(hwnd, self._syscmd_proc, 1)
                elif msg == 0x0112 and not self._quitting:            # WM_SYSCOMMAND
                    cmd = wp & 0xFFF0
                    if cmd == 0xF020 and not self.compact and self.mini_enabled():   # SC_MINIMIZE
                        later(self._mini_click)
                        return 0
                    if cmd == 0xF030 and self.compact:                  # SC_MAXIMIZE
                        later(self._full_click)
                        return 0
            except Exception as e:                                      # 窓の知らせの中で例外を出さない
                append_log(f"窓の操作の受け取りに失敗: {e!r}")
            return cc.DefSubclassProc(hwnd, msg, wp, lp)
        self._syscmd_proc = _SUBCLASSPROC(proc)                         # 消えないように持っておく
        cc.SetWindowSubclass.argtypes = [wt.HWND, _SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
        cc.RemoveWindowSubclass.argtypes = [wt.HWND, _SUBCLASSPROC, ctypes.c_size_t]
        cc.SetWindowSubclass(wt.HWND(int(form.Handle.ToInt64())), self._syscmd_proc, 1, 0)

    def _mini_click(self):
        with self._win_lock:
            if not self.compact and not self._quitting:
                self._enter_compact()

    def _full_click(self):
        with self._win_lock:
            if self.compact and not self._quitting:
                self._leave_compact()

    @staticmethod
    def _animate(form, on: bool):
        """窓の最小化・最大化・元に戻すのアニメーションを出す / 止める(小窓の出入りで窓の状態を変える間だけ止める)"""
        v = ctypes.c_int(0 if on else 1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(int(form.Handle.ToInt64())), 3, ctypes.byref(v), 4)   # DWMWA_TRANSITIONS_FORCEDISABLED

    @staticmethod
    def _placement(form):
        """窓の hwnd・Windows が持つ窓の配置(WINDOWPLACEMENT)・作業領域の座標のずれ(通常の位置は作業領域の座標。タスクバーの分ずれる)"""
        import System.Windows.Forms as WF
        hwnd = wt.HWND(int(form.Handle.ToInt64()))
        p = _WINDOWPLACEMENT()
        p.length = ctypes.sizeof(p)
        ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(p))
        s = WF.Screen.PrimaryScreen
        return hwnd, p, (s.WorkingArea.X - s.Bounds.X, s.WorkingArea.Y - s.Bounds.Y)

    def _normal_rect(self, form) -> tuple[int, int, int, int]:
        """通常のときの位置と大きさ(画面の座標・物理の px)。Windows の記録から読む
        (WinForms の RestoreBounds は、_place で Windows に直接変えさせた後は古いまま)"""
        _, p, (dx, dy) = self._placement(form)
        r = p.rc
        return r.left + dx, r.top + dy, r.right - r.left, r.bottom - r.top

    def _place(self, form, maximized: bool, x: int, y: int, w: int, h: int):
        """窓の状態(通常 / 最大化)と通常のときの位置・大きさ(画面の座標・物理の px)を 1 回で変える。
        WindowState と Bounds を順に変えると、途中の大きさ(最大化 → 通常の大きさ → 小窓 など)が一瞬見える"""
        hwnd = wt.HWND(int(form.Handle.ToInt64()))
        if maximized:
            ctypes.windll.user32.ShowWindow(hwnd, 3)                          # 先に最大化(通常の大きさを経由しない)
        hwnd, p, (dx, dy) = self._placement(form)
        p.flags, p.showCmd = 0, 3 if maximized else 1                         # SW_SHOWMAXIMIZED / SW_SHOWNORMAL
        p.rc = wt.RECT(x - dx, y - dy, x - dx + w, y - dy + h)
        ctypes.windll.user32.SetWindowPlacement(hwnd, ctypes.byref(p))

    def _keep_mini(self, form, _e):
        """小窓の大きさが外から変えられたら戻す(位置はそのまま)。
        2026-11-24: 通常の窓で Win + D → 小窓 → もう一度 Win + D で、Windows が 1 回目に覚えた大きさを書き戻し、中身は小窓のまま広がった"""
        import System.Drawing as D
        import System.Windows.Forms as WF
        wh, xy = self._mini_wh, self._mini_xy
        if self.compact and wh and form.WindowState == WF.FormWindowState.Normal and (form.Width, form.Height) != wh:
            form.Bounds = D.Rectangle(*(xy or (form.Left, form.Top)), *wh)

    def _keep_mini_pos(self, form, _e):
        """小窓の位置を覚える(大きさが小窓のときだけ。外から大きさと一緒に動かされたら _keep_mini で戻す)"""
        import System.Windows.Forms as WF
        if self.compact and form.WindowState == WF.FormWindowState.Normal and (form.Width, form.Height) == self._mini_wh:
            self._mini_xy = (form.Left, form.Top)

    def _leave_compact(self):
        """小窓をやめて、小窓にする前の窓(大きさ・位置・最大化)に戻す。小窓の位置を保存する"""
        import System.Drawing as D
        import System.Windows.Forms as WF
        x, y, w, h, was_max = self._normal
        box = {}

        def go(f):
            self._animate(f, False)
            if f.WindowState == WF.FormWindowState.Normal:
                box["pos"] = [int(f.Left), int(f.Top)]
            elif f.RestoreBounds.Width:
                box["pos"] = [int(f.RestoreBounds.X), int(f.RestoreBounds.Y)]
            self._apply_top(f, False)
            if f.WindowState != WF.FormWindowState.Normal:   # 最大化・最小化された小窓(タイトルバーのボタン以外)は、先に通常へ
                f.WindowState = WF.FormWindowState.Normal
            f.FormBorderStyle = WF.FormBorderStyle.Sizable
            self._place(f, was_max, x, y, w, h)              # 途中の大きさを挟まずに、元の大きさ・最大化へ
            f.Bounds = D.Rectangle(x, y, w, h)               # 最大化のときは WinForms が覚える「元に戻す大きさ」になる(しないと小窓の大きさに戻る)
            if self._min_size is not None:
                f.MinimumSize = self._min_size               # 大きさを戻した後に(先にすると、いったん最小の大きさまで広がる)
            f.Activate()
            self._animate(f, True)
        self.compact = False
        self.emit("compact", False, replace=True)
        self._form(go)
        self.maximized, self.minimized = was_max, False
        if box.get("pos"):
            self.settings["mini_pos"] = box["pos"]
            self._save_settings({"mini_pos": box["pos"]})

    def show_full(self):
        """確かめる窓・知らせの窓を出す前に: 小窓・最小化なら元の窓に戻す"""
        with self._win_lock:
            if self.compact:
                self._leave_compact()
            elif self.minimized:
                self._form(lambda f: setattr(f, "WindowState", f.WindowState.Maximized if self.maximized else f.WindowState.Normal))
                self._form(lambda f: f.Activate())

    def _closed(self):
        """主の窓が閉じた: 送るのを止める"""
        self._quitting = True
        self.bridge.stop()

    def start(self, func=None):
        """窓を出して閉じるまで待つ(func(窓) は窓ができたあと別の糸で呼ぶ: テスト用)"""
        import webview
        self.create_window()
        storage = os.path.join(os.path.dirname(os.path.abspath(self.settings_path)), "webview")
        clear_cache(storage)
        webview.start(func, self.window if func else None, private_mode=False, storage_path=storage)
        self.bridge.stop()

    def hello(self) -> dict:
        s = load_settings(self.settings_path)
        return {"ui": ui_values(), "zoom": self.zoom, "frozen": FROZEN,
                "layout": {k: s.get(k) for k in ("right_split", "ledger_col_widths", "refs_col_widths")}}

    def emit(self, kind, data=None, replace=False):
        self.bridge.emit(kind, data, replace)

    # ---- device_core.DeviceWatch の view
    def show_list(self, rows, selected, message, can_connect):
        self.emit("device_list", {"rows": rows, "selected": selected, "message": message, "can_connect": can_connect}, replace=True)

    def show_overlay(self, on):
        self.emit("device_overlay", bool(on), replace=True)

    def show_wait(self, on):
        self.emit("device_wait", bool(on), replace=True)

    def redraw(self):
        """本体画面を描き直す: つながっていれば届いた画面、切れている間は切れる直前の画面に半透明のグレー、無ければ白"""
        from PIL import Image
        img, shade = self.device.frame()
        if img is None:
            self.emit("frame", None, replace=True)
            return
        if shade:
            color, alpha = UI.DEVICE_SHADE
            img = Image.blend(img.convert("RGB"), Image.new("RGB", img.size, color), alpha)
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=1)
        self.emit("frame", {"png": base64.b64encode(buf.getvalue()).decode(), "w": img.width, "h": img.height,
                            "shade": shade}, replace=True)

    # ---- controller.Controller の view
    def status(self, title, colors=None):
        self.emit("status", {"title": title, **status_look(title, tuple(colors) if colors else None)}, replace=True)

    def detail(self, text):
        self.emit("detail", text, replace=True)

    def aside(self, values):
        self.emit("aside", values, replace=True)

    def progress(self, show, value):
        self.emit("progress", {"show": bool(show), "value": float(value)}, replace=True)

    def button(self, text, color, enabled):
        self.emit("button", {"text": text, "color": color, "enabled": bool(enabled)})

    def end_button(self, show, enabled):
        self.emit("end_button", {"show": bool(show), "enabled": bool(enabled)}, replace=True)

    def modes(self, payload):
        self.emit("modes", payload, replace=True)

    def lock_info(self, on, values):
        self.emit("info", {"locked": bool(on), "values": values}, replace=True)

    def lock_settings(self, on):
        self.emit("settings_lock", bool(on), replace=True)

    def confirm(self, title, lead, checks, ok_text):
        if self.compact or self.minimized:           # 確かめる窓は元の窓に戻して出す(小窓のボタンから始めたときも)
            threading.Thread(target=self.show_full, daemon=True).start()
        self.emit("confirm", {"title": title, "lead": lead, "checks": checks, "ok": ok_text, "icon": UI.DANGER["icon"]})

    def message(self, title, text):
        if self.compact or self.minimized:
            threading.Thread(target=self.show_full, daemon=True).start()
        self.emit("message", {"title": title, "text": text})

    def log(self, text):
        self.emit("log", text)

    def alert(self, title, desc=""):
        self.emit("alert", [title, desc])

    def select_log(self):
        self.emit("select_log")

    def manual(self, on):
        self.emit("manual", bool(on), replace=True)

    def ledger(self, payload):
        self.emit("ledger", payload, replace=True)

    def refs_data(self, data):
        self.emit("refs_data", data, replace=True)

    def refs_view(self, view):
        self.emit("refs_view", view, replace=True)

    def roles(self, roles):
        self.emit("roles", roles, replace=True)

    def _save_settings(self, data: dict):
        with self._save_lock:                        # 読んで混ぜて書く間に、ほかの糸の保存が割り込まないように
            save_settings({**load_settings(self.settings_path), **data}, self.settings_path)

    # ---- 閉じる
    def _closing(self):
        """窓を閉じる: 測定中なら確かめてから(照射を止めて終了)。照射中ならトリガーを離してから、設定を保存し、機器との接続を切る。
        照射を止められなければ知らせる。False を返すと閉じない"""
        if self.bridge.run(self.ctl.running):
            try:
                if not self.window.create_confirmation_dialog(UI.CLOSE_CONFIRM_TITLE, UI.CLOSE_CONFIRM_TEXT):
                    return False
            except Exception:
                pass
        self.bridge.closed.set()                     # これより後は画面へ送らない(閉じていく窓へ送ると .NET のエラーになる)
        released = self.bridge.run(self.shutdown, timeout=30)
        if not released:
            try:
                self.window.create_confirmation_dialog(UI.CLOSE_FAIL_TITLE, UI.CLOSE_FAIL_TEXT)
            except Exception:
                pass
        return True

    def shutdown(self) -> bool:
        """閉じる前の後始末(画面の糸)。照射が止まった・照射していなければ True"""
        released = self.ctl.shutdown()
        self._save()
        return released

    def _save(self):
        try:
            data = load_settings(self.settings_path)
            win = self.window
            if self.compact:                         # 小窓の間: 元の窓の大きさは変えず、小窓の位置だけ残す
                if not self.minimized:
                    data["mini_pos"] = [int(win.native.Left), int(win.native.Top)]
                data["web_zoomed"] = bool(self._normal and self._normal[4])
                save_settings(data, self.settings_path)
                return
            if win is not None and not self.maximized and not self.minimized:   # 最小化の間の位置は画面の外
                data["web_window"] = [int(win.width), int(win.height), int(win.x), int(win.y)]
            data["web_zoomed"] = self.maximized
            save_settings(data, self.settings_path)
        except Exception:
            append_log("[設定を保存できない] " + traceback.format_exc(limit=2))


def selftest(app: WebApp, window):
    """--selftest: 起動して、画面が読めて やり取りができるかを確かめ、config/web_selftest.txt に結果を書いて閉じる
    (別の PC で exe が動くかの確認用。機器には触れない)"""
    out, t0 = [], time.time()
    ok = lambda name, cond: out.append(f"{'OK' if cond else 'NG'}  {name}")
    try:
        ok("画面を読み、見た目の値を受け取った", app.bridge.ready.wait(30)
           and window.evaluate_js("app.UI && app.UI.TITLE") == UI.TITLE)
        for n in range(50):
            app.bridge.emit("test", n)
        end = time.time() + 10
        while time.time() < end and window.evaluate_js("app.stats.order.length") < 50:
            time.sleep(0.1)
        ok("知らせが順番どおりに届く", window.evaluate_js("app.stats.order.every((v, i) => v === i) && app.stats.order.length === 50"))
        ok("左の列: 状態の欄は「未接続」・測定モード表を出した",
           window.evaluate_js("app.state.status && app.state.status.title") == UI.DEVICE_STATUS["offline"][1]
           and window.evaluate_js("document.querySelectorAll('#modes tbody tr').length") >= 1)
        ok(f"起動から {time.time() - t0:.1f} 秒(exe: {FROZEN}・倍率 {app.zoom})", True)
    except Exception as e:
        ok(f"思わぬエラー {e!r}", False)
    try:
        from storage import CONFIG_DIR, write_atomic
        write_atomic(os.path.join(CONFIG_DIR, "web_selftest.txt"), "\n".join(out) + "\n", encoding="utf-8")
    finally:
        window.destroy()


def main():
    am.use_base()                  # 相対パス(config / TracerData)の基準を VBS(exe)のフォルダにする
    if "--selftest" not in sys.argv and not single_instance():   # アプリは 1 つだけ(自己確認は機器に触れないので除く)
        found = show_running(UI.WEB_TITLE)
        append_log(f"---- 起動しなかった(すでに起動している。開いている窓を{'最大化した' if found else '見つけられなかった'})")
        raise SystemExit(0)
    dl.AUTO_CONNECT = False        # 機器につなぐのは本体画面の「接続」だけ(2 段目)
    atexit.register(dl.close_link)
    append_log("---- 起動(WebView2 版)")
    try:
        test = "--selftest" in sys.argv
        if test:                                     # 自己確認では機器を探さない(機器に触れない)
            import device_core
            device_core.find_devices = lambda: []
        app = WebApp()
        app.start((lambda w: selftest(app, w)) if test else None)
    except Exception:
        append_log("[思わぬエラーで終了] " + traceback.format_exc())
        raise
    finally:
        dl.close_link()
        append_log("---- 終了(WebView2 版)")


if __name__ == "__main__":
    main()
