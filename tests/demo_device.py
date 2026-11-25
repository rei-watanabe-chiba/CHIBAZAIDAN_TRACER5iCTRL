"""偽の機器(デモ・テスト用。開発用。配布しない)。Tk を使わない

- 機器の代わりに PC の中の偽の機器(fake_device)につなぎ、本体画面には見本の絵(src/screens・tests/fixtures)を出す。
  本物の機器には一切つながず、X線も出ない。
- 測定の処理(auto_measure.run_plan・measure・失敗の判定・停止・再開)は本物を動かし、画面を読む・押す部分(切替・時間の書き込み・
  メイン画面へ戻す・ログイン・結果画面の読み取り)とトリガーだけを、状態を持つ偽の機器(DemoDevice・DemoOperator)に置き換える
  (install)。照射の時間は「速さ」倍で進む。
- 使うもの: tests/demo_web.py(デモ)・tests/web_smoke_test.py・tests/controller_test.py
- 2026-11-20: Tk 版のデモ(demo_gui.py)から、Tk を使わない部分を移した
"""
from __future__ import annotations

import os, shutil, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "Traer5iAuto", "src")
sys.path[:0] = [SRC, os.path.dirname(os.path.abspath(__file__))]

from PIL import Image

import auto_measure as am
import device_link as dl
from gui_config import UI
import storage
from fake_device import FakeDevice
import lid_sensor

LID = lid_sensor._SENSOR = lid_sensor.Fixed(lid_sensor.CLOSED)   # 偽の蓋のセンサー(「デモの操作」で開け閉めする)

SCREENS = os.path.join(SRC, "screens")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
KEEP = ("plan", "apps", "phase_counts", "set_each", "ledger_values", "ledger_col_widths", "refs", "right_split", "zoom")
ALIAS = "デモ機器"
# 画面の切り替えにかける時間(秒。「遅く」で 3 倍)。停止を押しやすいように本物より少し長め
T_SELECT, T_CONFIRM, T_FIELD, T_KEY, T_BACK = 1.5, 1.0, 0.6, 1.2, 0.4
RESULT_FILES = {"Glass Dual": os.path.join(FIXTURES, "result_glass_dual.png"),
                "Spectrometer Mode": os.path.join(FIXTURES, "result_spectrometer.png")}
INJECTS = [   # 次の照射で起こすこと: (キー, 表示)
    ("", "なし(ふつうに測る)"),
    ("cut", "接続エラー: 照射の途中で USB が抜ける"),
    ("empty", "機器エラー: 計数率が低すぎます(元素の表が空)"),
    ("title", "機器エラー: 結果画面の題名が違う"),
    ("odd", "機器エラー: 測定中に想定外の画面"),
    ("short", "停止エラー: 本体で照射が止まる(設定の半分の時間)"),
]


def image(kind: str, app: str = "") -> Image.Image | None:
    """見本の画面。kind: main / select_app / confirm / settings / keypad / measuring / result / empty / unknown / login / startup"""
    a = app.replace(" ", "_")
    names = {"main": [f"main_{a}", "main"], "select_app": [f"select_{a}", "select_app"], "confirm": ["confirm"],
             "settings": [f"settings_{a}", "settings_Mudrock_Air"], "keypad": ["keypad"],
             "measuring": [f"measuring_{a}", "measuring_start"], "result": [f"result_{a}", "result_Mudrock_Air"],
             "login": ["login"], "startup": ["startup"]}.get(kind, [])
    if kind == "result" and app in RESULT_FILES:
        return Image.open(RESULT_FILES[app])
    if kind == "empty":
        return Image.open(os.path.join(FIXTURES, "result_low_count.png"))
    if kind == "unknown":
        return Image.open(os.path.join(FIXTURES, "main_inoperable.png"))
    for n in names:
        p = os.path.join(SCREENS, n + ".png")
        if os.path.exists(p):
            return Image.open(p)
    return None


class DemoDevice:
    """偽の機器の状態: 画面・モード・トリガー・USB・機器の測定時間・照射・結果表。測定はここで進む(tick)"""

    def __init__(self, plan: list):
        self.fd: FakeDevice | None = None
        self.screen, self.app = "main", plan[0][0] if plan else "Obsidian"
        self.trig = "pull"                          # Operator の言い方: "pull" = 引かれていない / "release" = 引かれている(照射中)
        self.present = True                          # USB がある
        self.offline = False                         # 機器との接続が切れている(画面を読むと ConnectionError)
        self.times = {**{a: list(map(float, p if isinstance(p, list) else [p])) for a, p in UI.DEFAULT_TIMES.items()},
                      **{a: list(map(float, p if isinstance(p, list) else [p])) for a, p, *_ in plan}}
        self.no, self.records, self.result = 900, [], {}
        self.meas: dict | None = None               # 照射中: {"t0", "dur", "app", "inject"}
        self.inject = ""                            # 次の照射で起こすこと(INJECTS のキー)
        self.keep_inject = False                    # 毎回起こす(使っても「なし」に戻さない)
        self.login_after = False                    # USB を差し直したらログイン画面(結果画面が消える)
        self.auto_plug = True                       # 接続エラーのあと、自動で USB を差す
        self.cut_at = 50                            # 接続エラー: 照射の何 % で USB が抜けるか
        self.speed = 10                             # 照射の速さ(倍)
        self.slow = False                           # 画面の切り替えを遅く(3 倍)
        self.lock = threading.RLock()

    # ---- 画面
    def go(self, screen: str, image_kind: str | None = None, app: str | None = None):
        with self.lock:
            self.screen = screen
            img = image(image_kind or screen, app or self.app)
        fd = self.fd
        if img is not None and fd is not None and not self.offline:
            threading.Thread(target=fd.show, args=(img,), daemon=True).start()

    def wait_t(self, t: float) -> float:
        return t * (3 if self.slow else 1)

    # ---- 照射
    def start(self):
        with self.lock:
            if self.screen != "main":
                raise am.DeviceError(f"デモ: メイン画面でないのでトリガーを引けません(現在: {self.screen})")
            inject = self.inject
            if not self.keep_inject:
                self.inject = ""
            self.trig = "release"
            self.meas = {"t0": time.time(), "dur": sum(self.times.get(self.app, [30.0])), "app": self.app, "inject": inject}
        self.go("measuring")
        self._say("TRIGGER_PULLED")
        if inject == "cut":                         # 照射の途中(cut_at %)で USB が抜ける
            threading.Timer(self.meas["dur"] / self.speed * self.cut_at / 100, self.pull_usb).start()

    def elapsed(self) -> float:
        m = self.meas
        return (time.time() - m["t0"]) * self.speed if m else 0.0

    def tick(self):
        """照射の進み: 設定の時間で自動で止まる。起こすこと(本体で止まる・想定外の画面)もここで"""
        with self.lock:
            m = self.meas
            if not m:
                return
            el = self.elapsed()
            if m["inject"] == "odd" and el >= m["dur"] / 3:
                if self.screen == "measuring":
                    self.go("unknown")                  # 機器のエラー表示のまま止まる(アプリが機器エラーにしてトリガーを離す)
                return
            if m["inject"] == "short" and el >= m["dur"] / 2:
                self.finish(m["dur"] / 2)
            elif el >= m["dur"]:
                self.finish(m["dur"])

    def finish(self, inst: float):
        """照射が終わった(自動で・トリガーを離した・USB が抜けた): 結果画面にして、機器の結果表に1行"""
        with self.lock:
            m, self.meas = self.meas, None
            if not m:
                return
            self.trig = "pull"
            self.no += 1
            inject = m["inject"]
            title = "" if inject in ("empty", "title") else m["app"]
            self.result = {"sample_no": str(self.no), "inst_time": f"{inst:.1f}", "title": title,
                           "empty": inject == "empty"}
            # Spectrometer Mode の回は結果表に時間が無い(pdz だけ。results_rows が時間なしで足す)
            self.records.append({"File #": str(self.no), "Application": m["app"],
                                 "ElapsedTime": "" if m["app"] == "Spectrometer Mode" else f"{inst:.1f}"})
            kind = "empty" if inject == "empty" else "result"
            show_app = ("Obsidian" if m["app"] != "Obsidian" else "Mudrock Air") if inject == "title" else m["app"]
        self.go("result", kind, show_app)
        self._say("TRIGGER_RELEASED")

    # ---- USB
    def pull_usb(self):
        """USB を抜く: 照射中なら機器は照射を止める(その時間で結果表に残る)。接続はすぐ切れる"""
        with self.lock:
            if not self.present:
                return
            if self.meas:
                self.finish(self.elapsed())
            self.present, self.offline = False, True
            fd, self.fd = self.fd, None
        if fd is not None:
            fd.drop()
        if self.auto_plug:
            threading.Timer(3.0, self.plug_usb).start()

    def plug_usb(self):
        """USB を差す: 新しい偽の機器(画面は今の画面。差し直したらログイン画面にする設定ならログイン画面)"""
        with self.lock:
            if self.present:
                return
            if self.login_after:
                self.screen = "login"
            kind = {"result": "result"}.get(self.screen, self.screen)
            img = image("empty", self.app) if self.result.get("empty") and kind == "result" else image(kind, self.app)
            self.fd = FakeDevice(screen=img).start()
            self.present, self.offline = True, False

    def _say(self, status: str):
        fd = self.fd
        if fd is not None and not self.offline:
            threading.Thread(target=lambda: _quiet(fd.say, status), daemon=True).start()


def _quiet(f, *a):
    try:
        f(*a)
    except Exception:
        pass


class DemoOperator:
    """auto_measure.Operator の代わり。画面は DemoDevice の状態({"state", "info"})"""

    def __init__(self, dev: DemoDevice):
        self.dev, self.ui = dev, None
        self.rc = type("RC", (), {"relink": lambda _: bool(dl.IN_USE and dl.current_link() is not None and not dev.offline)})()

    def read(self):
        if self.dev.offline:
            raise ConnectionError("機器との接続が切れました(デモ: USB が抜けています)")
        self.dev.tick()
        self.ui = {"state": self.dev.screen, "info": dict(self.dev.result)}
        return self.ui

    def state(self, ui=None) -> str:
        return (ui if ui is not None else self.read())["state"]

    def wait_state(self, want, timeout, interval=0.6):
        want = (want,) if isinstance(want, str) else tuple(want)
        end = time.time() + timeout
        while True:
            ui = self.read()
            if ui["state"] in want:
                return ui
            if time.time() > end:
                raise am.OcrError(f"デモ: {want} になりません(現在: {ui['state']})")
            am._sleep(0.2)

    def current_app(self, ui=None) -> str:
        ui = ui if ui is not None else self.read()
        return self.dev.app if ui["state"] == "main" else ""

    def trigger_state(self) -> str:
        return "unknown" if self.dev.offline else self.dev.trig

    def trigger(self):
        if self.dev.offline:
            raise ConnectionError("機器との接続が切れました(デモ)")
        if self.dev.trig == "pull":
            self.dev.start()
        elif self.dev.meas:                        # 照射中に離した(停止・中断): 途中までの結果画面
            self.dev.finish(self.dev.elapsed())
        else:                                      # 手で引いていたのを離す
            self.dev.trig = "pull"
            self.dev._say("TRIGGER_RELEASED")

    def save_error(self):
        pass


def install(dev: DemoDevice, op: DemoOperator):
    """auto_measure の画面を読む・押す部分をデモの機器に置き換える(run_plan など流れの処理は本物)"""
    am.Operator = lambda *a, **k: op

    def to_main(_op):
        st = op.state()
        if st == "main":
            return
        if st == "login":
            am.login(op, am.PASSCODE)
        elif st == "startup":
            am.open_login(op)
        elif st in ("result", "select_app", "confirm", "settings"):
            am._sleep(dev.wait_t(T_BACK))
            dev.go("main")
        else:
            raise am.DeviceError(f"メイン画面ではありません(現在: {st})。手動でメイン画面に戻してください")
        if op.state() != "main":
            raise am.LoginWait("ログイン画面です") if op.state() == "login" else am.DeviceError(f"メイン画面にできません({op.state()})")
    am.to_main = to_main

    def switch_app(_op, app):
        am.to_main(op)
        if dev.app == app:
            return
        op.state(); dev.go("select_app", app=app); am._sleep(dev.wait_t(T_SELECT))
        op.state(); dev.app = app; dev.go("confirm"); am._sleep(dev.wait_t(T_CONFIRM))
        op.state(); dev.go("main")
        am.say(f"  アプリ切替: -> {app}")
    am.switch_app = switch_app

    def set_phase_times(_op, app, phases):
        am.to_main(op)
        op.state(); dev.go("settings")
        am._sleep(dev.wait_t(T_FIELD))
        changed = [float(p) for p in phases] != dev.times.get(app)
        if changed:
            for _ in phases:
                op.state(); dev.go("keypad"); am._sleep(dev.wait_t(T_KEY))
                op.state(); dev.go("settings"); am._sleep(dev.wait_t(T_FIELD))
            dev.times[app] = [float(p) for p in phases]
            am.say(f"  {app} の測定時間を設定: {am.fmt_phases(phases)} 秒")
        op.state(); dev.go("main")
        return changed
    am.set_phase_times = set_phase_times

    def login(_op, code):
        if op.state() != "login":
            return
        if not code:
            raise am.LoginWait("ログイン画面です。PASS を入れて「ログイン」")
        if not str(code).isdigit():
            raise am.LoginFailed("ログインできませんでした。PASS を確認してください")
        time.sleep(0.6)
        dev.go("main")
        am.say("  ログインしました")
    am.login = login
    am.open_login = lambda _op: dev.go("login")
    am.result_info = lambda ui: dict(ui["info"]) if isinstance(ui, dict) else {}
    am.log = lambda row: None                       # 実行記録(measure_log.csv)は書かない
