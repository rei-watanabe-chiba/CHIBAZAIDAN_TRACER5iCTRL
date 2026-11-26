"""WebView2 版の決まりの確認(窓を出さない。controller.Controller と device_core.DeviceWatch を直接動かす)。TRACERexl で実行する

    python tests/controller_test.py      (2〜3 分)

Tk 版の tests/gui_smoke_test.py で確かめている動きのうち、画面の作りによらないもの(状態とボタン・自動の検索と接続・ログイン・
起動中・手動動作中・入力の不備・組み合わせと再計測 / 再開・セットの終わり・終了・計測済み・セットの警告・停止・照射の途中の
接続切れ・取り込み・時間のずれ・閉じる)を、WebView2 版の中身で確かめる。画面の側は記録するだけの偽物(FakeView)。
機器はデモ版と同じ偽の機器(tests/demo_device.py の DemoDevice・DemoOperator。画面は tests/fake_device.py。127.0.0.1)だけ。
測定の流れ(auto_measure.run_plan)は本物。X線は出ない。本物の設定・台帳・ログは書き換えない(一時フォルダ)。
"""
from __future__ import annotations

import os, shutil, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "Traer5iAuto", "src"), os.path.join(ROOT, "tests")]
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import auto_measure as am                                   # noqa: E402
import controller as ctl_mod                                # noqa: E402
import demo_device as dg                                     # noqa: E402
import device_core as dc                                    # noqa: E402
import device_link as dl                                    # noqa: E402
import storage                                              # noqa: E402
import web_app as wa                                        # noqa: E402
from fake_device import FakeDevice                          # noqa: E402
from gui_config import UI                                   # noqa: E402

results: list[bool] = []


def check(name: str, cond, detail=""):
    results.append(bool(cond))
    print(f"{'OK' if cond else 'NG'}  {name}" + ("" if cond else f"  -> {detail}"), flush=True)


def wait_for(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


# ---------------------------------------------------------------- 一時フォルダ・速くする値
tmp = tempfile.mkdtemp(prefix="controller_test_")
settings = os.path.join(tmp, "config", "gui_settings.json")
logs: list[str] = []
ctl_mod.append_log = wa.append_log = lambda line: logs.append(line)
am.TRACER_DIR = os.path.join(tmp, "TracerData")
am.LEDGER_STORE = os.path.join(tmp, "config", "ledger.json")
am.LOG_CSV, am.ERROR_PNG = os.path.join(tmp, "config", "measure_log.csv"), os.path.join(tmp, "config", "last_error.png")
am.SWITCH_WAIT_S = 0.1
for k in ("T_SELECT", "T_CONFIRM", "T_FIELD", "T_KEY", "T_BACK"):
    setattr(dg, k, 0.02)
UI.BOOT_POLL_MS, UI.CHECK_MAX_S, UI.CHECK_RETRY_S, UI.MAIN_BUTTON_LOCK_MS = 300, 3, 1, 300
PLAN = [["Obsidian", [1]], ["Mudrock Air", [1, 1]]]
storage.save_settings({"plan": PLAN, "set_each": True, "pass": ""}, settings)

# ---------------------------------------------------------------- 偽の機器(本物の機器には一切つながない)
dev = dg.DemoDevice(PLAN)
dev.speed = 20
dev.fd = FakeDevice(screen=dg.image("main", dev.app)).start()
op = dg.DemoOperator(dev)
dg.install(dev, op)
FOUND = [{"host": "127.0.0.1", "source": None, "alias": "テスト機器"}]
found = {"list": list(FOUND)}
dc.find_devices = lambda: list(found["list"]) if dev.present else []
dl.find_s1_titan = lambda pending=False: list(FOUND)
refuse = {"on": False}


def connect(host=None, source=None, refused_wait=None):
    assert host in (None, "127.0.0.1")
    if refuse["on"] or not dev.present or dev.fd is None:
        raise ConnectionRefusedError("テスト: つながらない")
    lk = dl.LINK
    if lk is None or not lk.connected or lk.ports["screen"] != dev.fd.ports[0]:
        if lk is not None:
            lk.close(abort=True)
        lk = dl.DeviceLink("127.0.0.1", timeout=5, ports=dev.fd.ports)
        lk.connect()
        dl.LINK = lk
    return lk
dc.connect_link = connect
ctl_mod.results_rows = lambda **k: list(dev.records)
fetched, fetch_fail = [], {"on": False}


def fetch(folder, nos, **k):
    if fetch_fail["on"]:
        raise OSError("テスト: 取り込めない")
    fetched.append(list(nos))
    return [f"{n}.pdz" for n in nos] + ["Results.csv"], []
ctl_mod.fetch_measurements = fetch
dl.NOTE = lambda *_: None
dl.AUTO_CONNECT = False


class FakeView:
    """画面の側の偽物: 届いたものを覚えるだけ"""

    def __init__(self):
        self.s: dict = {}
        self.logs, self.alerts, self.messages, self.confirms = [], [], [], []

    def __getattr__(self, name):                       # 覚えるだけの知らせ(button・status・detail …)
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a: self.s.__setitem__(name, a if len(a) != 1 else a[0])

    def log(self, text): self.logs.append(text)
    def alert(self, title, desc=""): self.alerts.append((title, desc))
    def message(self, title, text): self.messages.append((title, text))
    def confirm(self, *a): self.confirms.append(a)
    def button(self, text, color, enabled):            # 押せるかだけを変える知らせ(文字は None)では文字を残す
        prev = self.s.get("button", (None, None, False))
        self.s["button"] = (text if text is not None else prev[0], color if color is not None else prev[1], enabled)

    def show_list(self, rows, selected, message, can_connect):
        self.s["list"] = {"rows": rows, "selected": selected, "message": message, "can_connect": can_connect}


bridge = wa.Bridge()                                        # 画面の糸(窓は無いので画面へは送らない)
view = FakeView()
after = lambda ms, fn, *a: bridge.after(ms, fn, *a)
post = lambda fn, *a: bridge.run(fn, *a, wait=False)
C = None
cb = lambda name: (lambda *a: getattr(C, name)(*a))
D = dc.DeviceWatch(view, after, alert=cb("_alert"), on_busy=cb("device_busy"), on_connected=cb("device_connected"),
                   on_lost=cb("device_lost"), on_trigger=cb("trigger_changed"), on_booted=cb("device_booted"),
                   note=lambda m: logs.append(m))
saved = {}
C = ctl_mod.Controller(view, after, post, storage.load_settings(settings),
                       lambda d: (saved.update(d), storage.save_settings({**storage.load_settings(settings), **d}, settings)), D)
call = lambda fn, *a: bridge.run(fn, *a, timeout=60)
call(lambda: (C.start(storage.load_settings(settings)), D.start()))

title = lambda: view.s.get("status", ("",))[0]
button = lambda: view.s.get("button", (None, None, False))           # (文字, 色, 押せるか)
btn = lambda: (C.btn_mode, C.btn_enabled)
detail = lambda: view.s.get("detail", "")
states = lambda: [r["no"] for r in view.s["modes"]["rows"]]
rows = lambda: list(C.ledger.book.rows.values())
idle = lambda: not C.running() and C.run_turn is None and not C.status_busy
T = UI.DEVICE_STATUS


def start_and_confirm(remeasure_ok=True):
    """メインのボタン → 照射前の確認 → 安全確認をチェックして照射開始"""
    n = len(view.confirms)
    wait_for(lambda: C.btn_enabled, 5)
    call(C.main_clicked)
    if not wait_for(lambda: len(view.confirms) > n, 10):
        return False
    return call(C.confirm_answer, True, [True])


def finish(timeout=60):
    return wait_for(lambda: idle() and not C._queued, timeout)


def set_values(**v):
    call(C.ledger.set_values, v)


try:
    # ================================================================ 状態とボタン・自動の検索
    check("起動: 未接続・押せない「▶ 自動計測」", wait_for(lambda: title() == T["offline"][1]) and btn() == ("busy", False)
          and button()[0] == UI.MAIN_BUTTON["start"][0])
    found["list"] = FOUND + [{"host": "127.0.0.1", "source": None, "alias": "テスト機器2"}]
    check("自動の検索: 2台以上なら確かめずに一覧に出し(状態は —)、選ぶまで「接続」は押せない",
          wait_for(lambda: len(view.s["list"]["rows"]) == 2 and all(r["state"] == UI.DEVICE_ROW_STATE["multi"] for r in view.s["list"]["rows"]), 5)
          and not view.s["list"]["can_connect"], view.s.get("list"))
    call(D.select, "テスト機器2")
    check("自動の検索: 1台を選ぶと「接続」を押せる", view.s["list"]["can_connect"] and view.s["list"]["selected"] == "テスト機器2")
    found["list"] = []
    check("自動の検索: USB が外れたら一覧から消して「機器が検知できません」",
          wait_for(lambda: not view.s["list"]["rows"] and view.s["list"]["message"] == UI.DEVICE_NONE, 5))
    refuse["on"] = True
    found["list"] = list(FOUND)
    check("自動の検索: USB が現れたら一覧に出し、確かめている間は「確認中…」・「接続」は押せない",
          wait_for(lambda: view.s["list"]["rows"] and view.s["list"]["rows"][0]["state"] == UI.DEVICE_ROW_STATE["booting"], 5)
          and not view.s["list"]["can_connect"])
    check("自動の検索: 上限までつながらなければ「接続不可」・「つながりませんでした」",
          wait_for(lambda: view.s["list"]["rows"][0]["state"] == UI.DEVICE_ROW_STATE["failed"], 10)
          and view.s["list"]["message"] == UI.DEVICE_FAILED and not view.s["list"]["can_connect"])
    refuse["on"] = False
    check("自動の検索: 「接続不可」の後は間をあけて確かめ直し、つながれば「接続可」(選んでおく・案内)",
          wait_for(lambda: view.s["list"]["can_connect"], 15) and view.s["list"]["rows"][0]["state"] == UI.DEVICE_ROW_STATE["ready"]
          and view.s["list"]["rows"][0]["serial"] == "FAKE001" and view.s["list"]["message"] == UI.DEVICE_READY, view.s.get("list"))
    call(D.connect_found)
    check("接続: 「接続」でつなぎ、一覧を消して画面を出す・測定の再開はこの接続を待つ(IN_USE)",
          wait_for(lambda: D.alive(), 10) and view.s.get("show_overlay") is False and dl.IN_USE)
    check("状態: つながったら読み、計測ファイルが空なら「入力エラー」(Database・ID)・押せない「▶ 自動計測」",
          wait_for(lambda: title() == UI.LBL_INPUT_ERROR, 10) and btn() == ("start", False)
          and UI.INPUT_ISSUES["required"].format(fields="Database・ID") in detail(), (title(), btn(), detail()))
    set_values(Database="DB", ID="A", subID="1")
    check("状態: 入力すると「準備OK」・アプリ名・変わった直後は少し押せない", wait_for(lambda: title() == T["ready"][1])
          and dev.app in detail() and btn() == ("start", False))
    check("状態: 少し経つと「▶ 自動計測」を押せる", wait_for(lambda: btn() == ("start", True), 3))
    check("状態の欄: 右に計測資料(Database・ID)", view.s.get("aside") == {"Database": "DB", "ID": "A"}, view.s.get("aside"))

    # ================================================================ ログイン・起動中・手動動作中
    dev.go("login")
    call(C._refresh_status)
    check("ログイン: ログイン画面ではボタンは「ログイン」・PASS が空なら押せない",
          wait_for(lambda: idle() and title() == T["login_nopass"][1]) and btn() == ("login", False), (title(), btn()))
    call(C.set_pass, "12a34")
    check("PASS: 半角数字だけを使い、入れると「ログイン」を押せる・設定にそのまま保存する",
          C.pass_code == "1234" and btn() == ("login", True) and storage.load_settings(settings).get("pass") == "1234")
    call(C.main_clicked)
    check("ログイン: 「ログイン」で PASS でログインし、状態を読む(準備OK)", wait_for(lambda: idle() and title() == T["ready"][1], 10)
          and dev.screen == "main", (title(), dev.screen))
    dev.go("startup")
    call(C._refresh_status)
    wait_for(lambda: idle() and btn()[0] == "login")
    call(C.main_clicked)
    check("ログイン: 起動画面なら「ログイン」を押してログイン画面へ、PASS でログイン", wait_for(lambda: idle() and title() == T["ready"][1], 10),
          (title(), dev.screen))
    dev.screen = "booting"
    call(C._refresh_status)
    check("起動中: 「起動中」・ボタンは押せない", wait_for(lambda: idle() and title() == T["booting"][1]) and btn() == ("busy", False))
    dev.go("main")
    check("起動中: 数秒おきに読み直し、メイン画面になったら「準備OK」", wait_for(lambda: title() == T["ready"][1], 5), title())
    dev.trig = "release"
    dev.fd.say("TRIGGER_PULLED")
    check("手動動作中: トリガーの状態の知らせが変わったら読み直し、手で引いていれば「手動動作中」・押せない",
          wait_for(lambda: idle() and title() == T["trigger"][1], 5) and btn() == ("busy", False), (title(), btn()))
    dev.trig = "pull"
    dev.fd.say("TRIGGER_RELEASED")
    check("手動動作中: 離すと「準備OK」に戻る", wait_for(lambda: idle() and title() == T["ready"][1], 5))

    # ================================================================ 入力の不備
    wait_for(lambda: btn() == ("start", True), 3)          # 機器の状態で変わった直後の短い待ちが終わってから
    ids = call(C.modes.ids)
    call(C.modes.delete, ids)
    check("不備: 測定モードが無ければ「モード：測定モードなし」", wait_for(lambda: UI.INPUT_ISSUES["no_modes"] in detail())
          and title() == UI.LBL_INPUT_ERROR)
    call(C.modes.load, PLAN)
    call(C._check_inputs)
    rid = call(C.modes.ids)[1]
    call(C.modes.set_phases, rid, ["1", "x"])
    check("不備: フェーズの欄の不正な値は表に入れず「モード：フェーズ入力不備」", wait_for(lambda: UI.INPUT_ISSUES["phase"] in detail())
          and call(C.modes.row, rid)["p"][:2] == ["1", "1"])
    call(C.modes.revert, rid)
    check("不備: 戻すと機器の状態に戻る(準備OK)・入力で直ったときは待たずに押せる",
          wait_for(lambda: title() == T["ready"][1]) and btn() == ("start", True),
          (title(), btn(), C._btn_lock, C.input_issues, C.dev, C.status_busy))

    # ================================================================ 計測: 1 回目が失敗 → 一部済み → 再計測
    dev.inject = "empty"                                # 次の照射は「計数率が低すぎます」(機器エラー)
    check("照射確認: チェックの無い答えでは照射しない", (lambda: (wait_for(lambda: C.btn_enabled, 3), call(C.main_clicked),
          wait_for(lambda: len(view.confirms) == 1, 10), not call(C.confirm_answer, True, [False])))()[-1] and not C.running())
    wait_for(lambda: title() == T["ready"][1], 5)
    check("照射確認: 安全確認をチェックして照射開始で始まる", start_and_confirm() and C.running())
    check("計測: 段階・照射中は「☢ 照射中」・停止を押せる",
          wait_for(lambda: title() == UI.IRRADIATING["label"] and btn() == ("stop", True), 15), (title(), btn()))
    check("計測: 失敗の回はその場で測り直さず次の回へ進み、終わったら一部済みは開いたまま「▶ 再計測(残り 1 回)」と「終了」",
          finish() and states() == [UI.MODE_ROW_STATE["fail"], UI.MODE_ROW_STATE["done"]]
          and wait_for(lambda: btn() == ("remeasure", True), 5) and button()[0] == UI.MAIN_BUTTON["remeasure"][0]
          and view.s.get("end_button") == (True, True), (states(), btn(), button(), view.s.get("end_button")))
    check("失敗の回: 台帳に「機器エラー」・アラート「測定失敗」・File.csv に出さない",
          [r["failed"] for r in rows()] == [am.FAIL_DEVICE, ""] and any(a[0] == "測定失敗" for a in view.alerts)
          and rows()[0]["No"] not in open(os.path.join(am.TRACER_DIR, UI.LEDGER_FILE), encoding="utf-8-sig").read())
    check("取込: 測定がすべて終わってから 1 回だけ、測った No を取り込む", fetched == [[rows()[0]["No"], rows()[1]["No"]]], fetched)
    check("再計測待ち: 計測ファイルの欄・測定モード表は変えられない・状態の欄に未了", C.ledger.locked and C.modes.locked
          and UI.PENDING_NOTE.format(n=1, apps="Obsidian") in detail(), detail())
    set_values(ID="Z")
    call(C._apply_locks)
    check("再計測待ち: 欄の値が開いているセットと違えば、セットの値に戻す", C.ledger.current_values()["ID"] == "A")
    check("再計測: 未了の行だけを測り、済みの行は ✓ のまま・同じセットの続き(S1-3)",
          start_and_confirm() and wait_for(lambda: states()[0] == UI.MODE_ROW_STATE["run"], 15)
          and states()[1] == UI.MODE_ROW_STATE["done"] and finish()
          and rows()[-1]["turn"] == rows()[0]["turn"] and rows()[-1]["seq"] == 3 and not rows()[-1]["failed"], (states(), rows()[-1]))
    check("セットの終わり: 全部済みなら ✓ ✓ のまま「↻ 画面更新」(メイン画面と読んでも)",
          states() == [UI.MODE_ROW_STATE["done"]] * 2 and wait_for(lambda: btn() == ("update", True), 5), (states(), btn()))
    call(C.main_clicked)
    check("セットの終わり: 「画面更新」でセットを閉じ、ID・subID・項目を空に(Database は残す)・番号に戻る",
          wait_for(lambda: idle() and C.closing is None, 10) and C.ledger.current_values()["ID"] == ""
          and C.ledger.current_values()["Database"] == "DB" and states() == ["1", "2"] and not C.modes.locked)

    # ================================================================ 計測済み・セットの警告
    set_values(ID="A", subID="1")
    check("計測済み: 同じ計測ファイル・モードの有効な行が台帳にあれば「計測済み(subID を変更)」で開始しない",
          wait_for(lambda: UI.INPUT_ISSUES["done"] in detail()) and btn() == ("start", False))
    set_values(subID="2")
    check("計測済み: subID を変えると測れる", wait_for(lambda: UI.INPUT_ISSUES["done"] not in detail() and btn() == ("start", True), 3))
    r_id = next(k for k, r in C.ledger.book.rows.items() if r["seq"] == 2)
    call(C.ledger.set_cell, r_id, "ID", "A-x")
    set_values(subID="1")
    check("セットの警告: 計測ファイルをセットと違う値に書き換えた行は「⚠ セット不一致」(薄い黄)・判定は変えない(計測済みのまま)",
          wait_for(lambda: "mismatch" in C.ledger.book.warn.get(r_id, [])) and C.ledger.book.shown(r_id)[2] == "warn"
          and wait_for(lambda: UI.INPUT_ISSUES["done"] in detail()), (C.ledger.book.warn, detail()))
    call(C.modes.load, [["Glass Dual", [1, 1]]])            # 台帳に無いモードなら計測済みでない → 状態の欄に注記
    call(C._check_inputs)
    check("セットの警告: 今の計測ファイルの行に警告があれば、状態の欄の説明に注記", wait_for(lambda: UI.TURN_WARN_NOTE in detail()), detail())
    call(C.ledger.set_cell, r_id, "ID", "A")
    check("セットの警告: 戻せば消える", wait_for(lambda: UI.TURN_WARN_NOTE not in detail()) and not C.ledger.book.warn)
    call(C.modes.load, PLAN)
    call(C._check_inputs)

    # ================================================================ 再開・終了(台帳の行から決める)
    set_values(ID="B", subID="1")
    tid = call(C.ledger.begin_turn, C.ledger.current_values(), ["Obsidian", "Mudrock Air"])
    call(C.ledger.add_measurement, "800", "Obsidian", "", tid)
    check("組み合わせ: 1 行目だけ有効な行と組みになる → ✓ ○、未了は測っていない回だけなので「▶ 再開(残り 1 回)」",
          wait_for(lambda: states() == [UI.MODE_ROW_STATE["done"], UI.MODE_ROW_STATE["wait"]] and btn()[0] == "resume", 5)
          and button()[0] == UI.MAIN_BUTTON["resume"][0], (states(), btn(), button()))
    call(C.end_turn)
    check("終了: 未了を測らずにセットを閉じ、済みの回も「✕ セット中断」で無効・入力値は残す・番号・「▶ 自動計測」",
          wait_for(lambda: btn()[0] == "start") and rows()[-1]["failed"] == UI.LEDGER_FAILED_ABORT
          and C.ledger.current_values()["ID"] == "B" and states() == ["1", "2"] and not C.ledger.locked
          and any(UI.END_TURN_LOG.format(n=1, k=1) in l for l in view.logs), (btn(), rows()[-1], states()))

    # ================================================================ 停止(照射中だけ)・時間のずれ・取り込みの失敗
    set_values(ID="C", subID="1")
    call(C.modes.load, [["Obsidian", [3]]])
    dev.speed = 1
    wait_for(lambda: btn() == ("start", True), 5)
    check("停止: 照射中になると押せる", start_and_confirm() and wait_for(lambda: btn() == ("stop", True), 15))
    call(C.main_clicked)
    check("停止: トリガーを離して止まり「停止エラー」・済みが 0 なら ✕・「画面更新」(再計測にしない)",
          finish(30) and dev.trig == "pull" and rows()[-1]["failed"] == am.FAIL_STOP
          and states() == [UI.MODE_ROW_STATE["fail"]] and wait_for(lambda: btn()[0] == "update", 5)
          and any(l.startswith("停止しました") for l in view.logs), (rows()[-1], states(), btn()))
    wait_for(lambda: btn() == ("update", True), 5)
    call(C.main_clicked)
    wait_for(lambda: idle() and C.closing is None, 10)
    check("停止: 「画面更新」で閉じた後、失敗の回は計測済みに数えない(同じ subID で「▶ 自動計測」)",
          wait_for(lambda: btn() == ("start", True), 5) and C.ledger.current_values()["ID"] == "C", (btn(), C.ledger.current_values()))

    # ================================================================ 蓋の磁気センサー(LID_SENSOR.md)
    set_values(ID="L", subID="1")
    wait_for(lambda: btn() == ("start", True), 5)
    dg.LID.set("open")
    check("蓋: 開くと「蓋なし」「蓋とセンサーを確認してください」・「自動計測」は押せない",
          wait_for(lambda: not btn()[1] and UI.INPUT_ISSUES["lid_open"] in detail() and title() == UI.LBL_LID_OPEN, 5), (btn(), title(), detail()))
    dg.LID.set("none")
    check("蓋: センサーが無ければ「センサー未接続」「蓋センサーを確認してください」・押せない",
          wait_for(lambda: not btn()[1] and UI.INPUT_ISSUES["lid_none"] in detail() and title() == UI.LBL_LID_NONE, 5), (btn(), title(), detail()))
    dg.LID.set("closed")
    check("蓋: 閉じると「自動計測」を押せる", wait_for(lambda: btn() == ("start", True), 5), btn())
    check("蓋: 照射中になる", start_and_confirm() and wait_for(lambda: btn() == ("stop", True), 15))
    n_alerts = len(view.alerts)
    dg.LID.set("open")
    check("蓋: 照射中に開いたらトリガーを離して止まり、その回は「機器エラー」・「蓋が開いて停止」・アラート「中断: 蓋開放」",
          finish(30) and dev.trig == "pull" and rows()[-1]["failed"] == am.FAIL_DEVICE
          and any(l.startswith(UI.LID_END) for l in view.logs)
          and any(a[0] == "中断: " + UI.ABORT_TEXT["lid"][0] for a in view.alerts[n_alerts:]),
          (rows()[-1], dev.trig, view.alerts[n_alerts:], view.logs[-3:]))
    dg.LID.set("closed")
    wait_for(lambda: btn() == ("update", True), 5)
    call(C.main_clicked)
    wait_for(lambda: idle() and C.closing is None, 10)
    dev.speed = 20
    call(C.set_skip_phases, True)
    dev.times["Obsidian"] = [2.0]                       # 機器の設定は 2 秒、表は 3 秒(書き込まない)
    fetch_fail["on"] = True
    n_alerts = len(view.alerts)
    check("ずれ: フェーズ設定をスキップして機器の時間が違えば、終わってから「計測時間誤差」(失敗にはしない)",
          start_and_confirm() and finish(30) and any(a[0] == "計測時間誤差" for a in view.alerts[n_alerts:])
          and not rows()[-1]["failed"], view.alerts[n_alerts:])
    check("取込: 取り込めなければアラート「取り込みエラー」", any(a[0] == "取り込みエラー" for a in view.alerts[n_alerts:]))
    fetch_fail["on"] = False
    call(C.set_skip_phases, False)
    wait_for(lambda: btn() == ("update", True), 5)
    call(C.main_clicked)
    wait_for(lambda: idle() and C.closing is None, 10)

    # ================================================================ 照射の途中で USB が抜ける(停止と同じ扱い・同じ機器が戻れば自動でつなぎ直す)
    set_values(ID="D", subID="1")
    call(C.modes.load, [["Obsidian", [4]], ["Mudrock Air", [1, 1]]])
    dev.speed, dev.inject, dev.cut_at, dev.auto_plug = 1, "cut", 40, True
    wait_for(lambda: btn() == ("start", True), 5)
    check("接続切れ: 照射の途中で切れたら「照射停止」で待つ・停止は押せない",
          start_and_confirm() and wait_for(lambda: title() == UI.RESUME_WAIT["cut"][0], 20) and btn() == ("stop", False),
          (title(), btn()))
    check("接続切れ: 同じ機器が戻れば自動でつなぎ直し、その回は「接続エラー」でセットを終える(次へ進まない)",
          finish(60) and rows()[-1]["failed"] == am.FAIL_CUT and states()[1] == UI.MODE_ROW_STATE["wait"]
          and any(l.startswith("接続切れで停止") for l in view.logs) and D.alive(), (rows()[-1], states(), D.alive()))
    dev.speed, dev.inject = 20, ""
    check("接続切れ: 済みが 0 なので「画面更新」", wait_for(lambda: btn()[0] == "update", 5), btn())
    wait_for(lambda: btn() == ("update", True), 5)
    call(C.main_clicked)
    wait_for(lambda: idle() and C.closing is None, 10)

    # ================================================================ 閉じる(照射中: トリガーを離して止める)
    set_values(ID="E", subID="1")
    call(C.modes.load, [["Obsidian", [5]]])
    dev.speed = 1
    wait_for(lambda: btn() == ("start", True), 5)
    start_and_confirm()
    wait_for(lambda: dev.trig == "release", 20)
    released = call(C.shutdown)
    check("閉じる: 測定中なら止め、トリガーを離し、接続を切る", released and dev.trig == "pull" and not C.running()
          and dl.current_link() is None, (released, dev.trig, C.running()))
    check("安全: 本物のトリガーの命令(TRIGGER_PULL)は偽の機器にも一度も送っていない(照射はデモの機器の中だけ)",
          "TRIGGER_PULL" not in (dev.fd.commands if dev.fd else []))
except Exception:
    import traceback
    check("思わぬエラー", False, traceback.format_exc())
    print("---- ログ(最後の 30 行)\n" + "\n".join(map(str, logs[-30:])))
finally:
    D.closing = True
    dl.close_link()
    if dev.fd is not None:
        dev.fd.drop()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if results and all(results) else 1)
