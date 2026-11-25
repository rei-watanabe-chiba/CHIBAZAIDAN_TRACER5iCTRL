"""測定 -> アプリケーション切替 -> 再測定 を繰り返す自動化

    python auto_measure.py --plan "Obsidian=30,Glass Dual=15+60,Obsidian=30"
        plan   : アプリケーション名=各フェーズの秒数(+区切り) をカンマ区切り。この順で1回ずつ測定する
                 (同じモードを繰り返すときは複数回並べる)
        --no-set   : 機器の測定時間を書き込まない(表の時間で待ち、ずれていれば知らせる)
                 各測定の前に、そのモードの設定画面へこの時間を書き込む(照射なし)
        照射時間は進捗表示と確認間隔の目安。機器が自動停止して結果画面になるまで確認を続ける
        --yes      : 開始時の確認プロンプトを省略

    python auto_measure.py --check                      # 画面状態を読むだけ(入力なし)
    python auto_measure.py --switch-only "Mudrock Air"   # アプリ切替だけ実行(F6なし・照射なし)
    python auto_measure.py --set-times "Glass Dual=15+60" # 測定時間を機器に書き込むだけ(照射なし)
    python auto_measure.py --login                      # GUI に保存した PASS で自動ログインだけ実行(照射なし)

前提: 機器が USB でつながっていること(直接つなぐ。RemoteCTRL などが機器につないでいたら閉じておく)。ログイン画面は PASSCODE で自動ログイン

ディスク使用:
  - 画面キャプチャはメモリ上のみ。保存しない
  - config/measure_log.csv に1測定1行の記録
  - 異常時のみ config/last_error.png を1枚(上書き)保存
"""
from __future__ import annotations

import csv, os, re, sys, threading, time

from screen_ui import ScreenUI, UIElement, same_name

# フォルダは start_measure_gui.vbs のフォルダを基準にした相対パス(storage.py)
from storage import ERROR_PNG, LEDGER_STORE, LOG_CSV, SETTINGS, TRACER_DIR, ensure_parent, load_settings, use_base  # noqa: F401
# (TRACER_DIR は controller が am.TRACER_DIR で使う。テストはここを差し替える)


class AbortError(Exception):
    pass


class Cancelled(AbortError):
    pass


# 中断の種類。GUI は種類(kind)ごとの短い文(UI.ABORT_TEXT)を出し、str(e) の詳しい原因はログに残す
class DeviceError(AbortError):
    kind = "device"          # 機器・画面の状態が想定と違う(押し直し・自動で戻すを試したあと)


class OcrError(AbortError):
    kind = "ocr"             # 画面の文字やボタンを見本と照合できない(読み直しを試したあと。名前は前の OCR のまま)


class LoginWait(AbortError):
    kind = "login_wait"      # ログイン画面だが PASS が無い


class LoginFailed(AbortError):
    kind = "login_failed"    # PASS が違う


class Overrun(AbortError):
    kind = "overrun"         # 設定時間を大幅に過ぎても終わらない(トリガーを離して中断)


class CutStop(AbortError):
    kind = "cut"             # 照射の途中で USB が切れて機器が照射を止めた: その回は失敗(接続エラー)にしてターンを終える(停止と同じ扱い)


class ManualTrigger(AbortError):
    kind = "manual"          # 測定の前からトリガーが引かれている(手で操作中)


class LidOpen(AbortError):
    kind = "lid"             # 蓋が開いている・センサーが無い(照射しない)/ 測定の途中で蓋が開いた(照射中ならトリガーを離した)


READ_RETRY = 3               # 読み取りに失敗したときの読み直し回数
TRACE = lambda msg: None     # 時間のかかり方をログのファイルにだけ残す(GUI が storage.append_log にする)
SETTLE_RETRY, SETTLE_S = 3, 0.5   # 見本で読めない画面(描きかけの画面など)は、この間隔で読み直す(機器へ直接つなぐとき)
FIND_WAIT_S = 4.0            # ボタンが見つからないとき、画面の遷移が遅れているとみなして読み直しながら待つ秒数


# ---- GUI等から差し替え可能なフック
CANCEL = threading.Event()                       # set() で停止要求
say = lambda msg: print(msg, flush=True)         # 進捗メッセージ出力先
progress = lambda elapsed, total: None           # 照射中の経過(秒)通知
phase_count_seen = lambda app, n: None           # 設定画面で数えたフェーズ数の通知


def _sleep(t: float):
    """停止要求に即応する sleep"""
    if CANCEL.wait(max(0.0, t)):
        raise Cancelled("ユーザーが停止しました")


# ---- 蓋の磁気センサー(LID_SENSOR.md)。LID_OK は「蓋が閉じているか」を答える関数(GUI・CLI が lid_sensor から入れる。
#      None なら確かめない: テストの一部だけ)。照射の直前に確かめ、測定の途中で開いたら lid_opened で止める
LID_OK = None
LID_TRIPPED = threading.Event()                  # 測定の途中で蓋が開いた
LID_AT = 0.0                                     # 蓋が開いた時刻(トリガーを離すまでの時間をログに残す)
_OP = None                                       # run_plan の Operator(蓋が開いたら、センサーの糸からすぐ止めるため)
RUNNING = threading.Event()                      # run_plan の途中


def lid_opened():
    """蓋が開いた(センサーの糸から呼ぶ)。測定の途中なら止める: 照射中はトリガーを離し(停止と同じ道)、その前なら照射しない"""
    global LID_AT
    if RUNNING.is_set():
        LID_AT = time.time()
        LID_TRIPPED.set()
        CANCEL.set()
        threading.Thread(target=_release_now, args=(_OP, LID_AT), daemon=True).start()   # 測定の糸を待たずに止める


def _release_now(op, t_open: float):
    """(別の糸)照射中なら、すぐ止める命令を送る(測定の糸は画面を読んでいる途中だと遅れるため)。止めるだけで、照射を
    始める方へは送らない。遅れの内訳(命令まで・OK まで・離れた知らせまで)をログのファイルに残す。接続が無い相手
    (テストの偽物など)なら何もしない(測定の糸が止める)"""
    link = getattr(getattr(op, "rc", None), "link", None)
    if link is None or link.status != "pulled":
        return
    try:
        link.release()
    except Exception as e:
        TRACE(f"[時間] 蓋が開いてすぐ止める命令を送れませんでした({e})。測定の糸が止めます")
        return
    ms = lambda t: f"{(t - t_open) * 1000:.0f} ms"
    TRACE(f"[時間] 蓋が開いてから: 止める命令まで {ms(link.release_sent)}"
          + (f"・OK まで {ms(link.release_ok)}" if link.release_ok >= link.release_sent else "・OK なし")
          + (f"・離れた知らせ(55101)まで {ms(link.status_at)}" if link.status_at >= link.release_sent else ""))


def stop_irradiation(op):
    """照射を止めるだけ(照射中でなければ何もしない)。機器へ直接つないでいれば、照射を始める方へは決して切り替えない
    (蓋の糸が先に止めた直後に、測定の糸が状態を読んでから送るまでの間に状態が変わっても安全)"""
    rc = getattr(op, "rc", None)
    if rc is not None and hasattr(rc, "release"):
        rc.release()
    elif op.trigger_state() == "release":          # テストの偽物(trigger は切り替えだけ)
        op.trigger()


def check_lid():
    """蓋が閉じていなければ照射しない"""
    if LID_OK is not None and not LID_OK():
        raise LidOpen("蓋が開いているか、蓋のセンサーが見つかりません")


BACK_STATES = ("select_app", "confirm", "settings", "result")   # GUI の「画面更新」でメイン画面へ戻す画面


def back_to_main(op) -> str:
    """GUI の「画面更新」: 途中の画面(アプリ選択・構成確認・設定・結果画面)ならメイン画面へ戻す。戻した後の画面の状態を返す。
    テンキー・測定中は戻さない(本体画面で閉じてから)"""
    st = op.state()
    if st in BACK_STATES:
        to_main(op)
        st = op.state()
    return st


def screen_state(ui: ScreenUI) -> str:
    """画面の状態: main / login / startup / booting / safety / select_app / confirm / keypad / settings / result / measuring / unknown"""
    if ui.has_text("試験準備完了"):
        return "main"
    if ui.has_text("ログインID") or ui.has_text("ログインD") or (ui.find_button("CLR", 0.9) and ui.find_button("DEL", 0.6)):
        return "login"
    if ui.has_text("初期化") and ui.find_button("ログイン", 0.8):
        return "startup"                               # 電源を入れて機器の OS が起動した直後の画面(「ログイン」でログイン画面へ)
    if ui.has_text("初期化中"):
        return "booting"                               # 起動の途中(進行のバー)。終わると起動画面になる
    if ui.has_text("トリガを引いて放すと続行"):
        return "safety"                                # 放射線の注意(電源を入れた後の最初のログインのあと。本体のトリガーを手で引いて放す)
    if ui.has_text("分析タイプを選択"):
        return "select_app"
    if ui.has_text("現在の構成") or (len(ui.buttons) <= 2 and ui.find_button("OK", 0.4) and ui.has_text("アプリケーション")):
        return "confirm"
    if any("クリア" in b.text and "削除" in b.text for b in ui.buttons):
        return "keypad"                                # 数値入力のテンキー画面
    if ui.has_text("フォルト設定を使用") or (ui.has_text("システム設定") and ui.has_text("期間")):
        return "settings"                              # 設定画面(「時間」の文字があるので measuring より先に判定)
    if (ui.find_button("平均を計算", 0.7) and ui.find_button("平均化", 0.8)) or (
            ui.find_button("情報の編集", 0.8) and not ui.find_button("ログオフ", 0.8) and not ui.find_button("カメラ", 0.8)):
        return "result"                                # 「情報の編集」は結果画面とメイン画面(Spectrometer Mode の結果画面は平均化が無い。
                                                       # 測定中の画面には無い。2026-11-13 実機)。メイン画面は「試験準備完了」で先に判定するが、
                                                       # 「作動できません」のメイン画面はその文字が無いので「ログオフ」「カメラ」で除く
    if any(t.text.startswith("時間") for t in ui.texts) or ui.find_button("スペクトル", 0.8):
        return "measuring"
    return "unknown"


# ================================================================ 操作
class Operator:
    """機器への入力と画面判定(機器へ直接つなぐ: device_link.DirectRemote)。座標は毎回その場で解析する"""

    def __init__(self):
        from device_link import DirectRemote
        self.rc = DirectRemote()
        self.ui: ScreenUI | None = None

    # ---- 画面
    def read(self) -> ScreenUI:
        from screen_match import recognized
        self.ui = self.rc.read_ui(self.rc.capture_background())
        for _ in range(SETTLE_RETRY):                     # 描きかけ(遷移の途中)の画面なら、少し待って読み直す
            if recognized(self.ui):
                break
            _sleep(SETTLE_S)
            self.ui = self.rc.read_ui(self.rc.capture_background())
        return self.ui

    def state(self, ui: ScreenUI | None = None) -> str:
        return screen_state(ui or self.read())

    def wait_state(self, want: str | tuple, timeout: float, interval=0.6) -> ScreenUI:
        want = (want,) if isinstance(want, str) else want
        t0 = time.time()
        end, self.seen = t0 + timeout, []          # 読んだ画面の並び(時間がかかったとき後から追えるように)
        while True:
            ui = self.read()
            st = self.state(ui)
            if not self.seen or self.seen[-1][0] != st:
                self.seen.append([st, time.time() - t0, 1])
            else:
                self.seen[-1][2] += 1
            if st in want:
                return ui
            if time.time() > end:
                raise DeviceError(f"画面 {want} を {timeout:.0f}秒待ちましたが、現在は '{st}' です")
            _sleep(interval)

    def current_app(self, ui: ScreenUI) -> str:
        b = ui.find_button("アプリケーション", 0.8)
        return b.text.replace("アプリケーション", "").strip() if b else ""

    # ---- トリガー(55100 の TRIGGER_PULL / RELEASE)
    def trigger_state(self) -> str:
        """'pull'(引ける=待機) / 'release'(引いている) / 'unknown'"""
        return self.rc.trigger_state()

    def trigger(self):
        """照射中なら止め、そうでなければ照射を始める"""
        self.rc.trigger()
        time.sleep(0.5)

    def find(self, label: str, *, any_kind=False, min_score=0.5):
        """ボタン(any_kind ならテキストも)を探す。見つからなければ、画面の遷移が遅れているとみなして
        読み直しながら FIND_WAIT_S 秒まで待つ(最低 READ_RETRY 回は読む)。それでも無ければ OcrError"""
        ui = self.ui or self.read()
        end, n = time.time() + FIND_WAIT_S, 1
        while True:
            e = ui.find_any(label, min_score) if any_kind else ui.find_button(label, min_score)
            if e is not None:
                return ui, e
            if n >= READ_RETRY and time.time() > end:
                break
            _sleep(0.5)
            ui, n = self.read(), n + 1
        raise OcrError(f"'{label}' が画面上に見つかりません({FIND_WAIT_S:g} 秒待ちました)")

    def click(self, label: str, *, any_kind=False, min_score=0.5):
        ui, e = self.find(label, any_kind=any_kind, min_score=min_score)
        self.click_elem(ui, e, wait=0.5)

    def click_until(self, label: str, want, timeout: float, *, any_kind=False, min_score=0.5) -> ScreenUI:
        """label を押して want の画面になるまで待つ。ならなければ1回だけ押し直す(押しそこね対策)"""
        before = self.state()
        self.click(label, any_kind=any_kind, min_score=min_score)
        try:
            return self.wait_state(want, timeout)
        except DeviceError:
            if self.state() != before:              # 別の画面に進んでいるなら押し直さない
                raise
            say(f"  「{label}」を押し直します")
            self.click(label, any_kind=any_kind, min_score=min_score)
            return self.wait_state(want, timeout)

    def click_elem(self, ui: ScreenUI, e, wait: float = 0.25):
        """解析済み画面の要素をクリック(機器へクリックを送る。実マウスは動かさない)"""
        cx, cy = self.rc.to_client(ui, e.center)          # 基準座標 → 機器の画面の座標
        self.rc.click(cx, cy)
        self.ui = None
        time.sleep(wait)

    def save_error(self):
        try:
            ensure_parent(ERROR_PNG)
            self.rc.capture_background().save(ERROR_PNG)
        except Exception:
            pass


# ================================================================ ログイン
PASSCODE = ""          # ログイン画面で入力する PASS(半角数字)。CLI(--login / --plan)だけが使う。GUI は「ログイン」ボタンで login() を直接呼ぶ
KEYPAD_LABELS = "123456789C0D"   # テンキーの並び(C=CLR, D=DEL) 3列×4行


def keypad(ui: ScreenUI) -> dict:
    """ログイン画面のテンキーを {文字: ボタン} で返す。読めなければ OcrError。
    数字1文字は OCR で読めないため、CLR/DEL を手がかりに同じ大きさのボタン12個を位置で割り当てる"""
    clr, dele = ui.find_button("CLR", 0.9), ui.find_button("DEL", 0.6)
    if not clr or not dele:
        raise OcrError("ログイン画面のテンキー(CLR/DEL)が見つかりません")
    w, h = clr.rect[2], clr.rect[3]
    keys = [b for b in ui.buttons if abs(b.rect[2] - w) <= w * 0.15 and abs(b.rect[3] - h) <= h * 0.15]
    keys.sort(key=lambda b: (round(b.rect[1] / h), b.rect[0]))
    if len(keys) != 12:
        raise OcrError(f"テンキーのボタン数が想定(12)と違います: {len(keys)}")
    m = dict(zip(KEYPAD_LABELS, keys))
    if m["C"] is not clr or m["D"] is not dele:
        raise OcrError("テンキーの配置が想定と違います")
    return m


def open_login(op: Operator):
    """起動画面(電源を入れた直後)の「ログイン」を押してログイン画面にする(照射なし・PASS は入れない)"""
    say("  起動画面のため「ログイン」を押します")
    t0 = time.time()
    op.click_until("ログイン", "login", 10, min_score=0.8)
    TRACE(f"[時間] 起動画面の「ログイン」→ ログイン画面 {time.time() - t0:.1f} 秒(読んだ画面: {_seen(op)})")


def _seen(op: Operator) -> str:
    return " → ".join(f"{st}×{n}({t:.1f})" for st, t, n in getattr(op, "seen", []))


def login(op: Operator, code: str):
    if not code or not code.isdigit() or not code.isascii():
        raise LoginWait("ログイン画面です。PASS(半角数字)を入力してください")
    for i in range(READ_RETRY):                 # テンキーが読めなければ読み直す
        ui = op.read()
        try:
            keys = keypad(ui)
            break
        except OcrError:
            if i == READ_RETRY - 1:
                raise
            _sleep(0.5)
    say("  ログイン画面のため自動ログインします")
    t0 = time.time()
    for ch in "C" + code:                      # CLR で入力欄を空にしてから入力
        op.click_elem(ui, keys[ch])
    _, ok = op.find("OK", min_score=0.4)
    op.click_elem(ui, ok)
    t1 = time.time()
    try:
        op.wait_state(("main", "result", "safety", "confirm"), 15)   # 電源を入れた後の最初は放射線の注意 → 現在の構成
    except DeviceError:
        raise LoginFailed("ログインできませんでした。PASS を確認してください")
    finally:
        TRACE(f"[時間] ログイン: テンキー {t1 - t0:.1f} 秒・OK の後 {time.time() - t1:.1f} 秒(読んだ画面: {_seen(op)})")
    say("  ログインしました")


# ================================================================ 手順
def to_main(op: Operator):
    """メイン画面にする。途中の画面なら自動で戻す(設定画面・アプリ選択 → キャンセル、構成確認 → OK、
    結果画面 → 戻る、ログイン → PASS で自動ログイン)。読めない画面は読み直す"""
    for _ in range(READ_RETRY + 1):
        st = op.state()
        if st == "main":
            return
        if st == "login":
            login(op, PASSCODE)
        elif st == "startup":
            open_login(op)                           # 起動画面 → ログイン画面(次の回でログインする)
        elif st in ("booting", "safety"):            # 起動の途中・放射線の注意: 測定の処理からは進めない(GUI が数秒おきに読み直す)
            raise DeviceError(f"機器が {'起動の途中' if st == 'booting' else '放射線の注意の画面(本体のトリガーを引いて放す)'} です")
        elif st == "result":
            # 「戻る」はOCRで誤読されやすいので、見つからなければ右下のボタンを押す
            ui = op.ui
            b = ui.find_button("戻る", 0.4) or max(ui.buttons, key=lambda e: (e.rect[1], e.rect[0]))
            op.click_elem(ui, b, wait=0.5)
            op.wait_state("main", 10)
        elif st in ("settings", "select_app"):
            say(f"  {st} 画面をキャンセルで閉じます")
            op.click("キャンセル", min_score=0.6)
            op.wait_state(("main", "confirm"), 10)
        elif st == "confirm":
            op.click("OK", min_score=0.4)
            op.wait_state("main", 15)
        elif st == "unknown":
            _sleep(1.0)                              # 読み違いかもしれないので読み直す
        else:                                        # 測定中・数値入力画面: 自動では戻さない
            raise DeviceError(f"メイン画面ではありません(現在: {st})。手動でメイン画面に戻してください")
    raise OcrError(f"メイン画面にできません(現在: {op.state()})")


def switch_app(op: Operator, app: str, retry: bool = True):
    """アプリ(モード)を切り替える。切替後の名前が違えば1回だけやり直す"""
    to_main(op)
    ui = op.read()
    if same_name(app, op.current_app(ui)):
        return
    say(f"  アプリ切替: {op.current_app(ui)} -> {app}")
    op.click_until("アプリケーション", "select_app", 10, min_score=0.8)
    for i in range(READ_RETRY):                         # リスト項目を選択(選択状態にならなければ押し直す)
        op.click(app, any_kind=True, min_score=0.6)
        ui = op.read()
        if any(same_name(app, b.text) for b in ui.buttons):
            break
        if i == READ_RETRY - 1:
            raise OcrError(f"'{app}' が選択状態になりません(一覧にない/スクロールが必要な可能性)")
    op.click_until("OK", "confirm", 10, min_score=0.4)
    op.click_until("OK", "main", 15, min_score=0.4)
    ui = op.read()
    if not same_name(app, op.current_app(ui)):
        if retry:
            say("  切替をやり直します")
            return switch_app(op, app, retry=False)
        raise OcrError(f"切替後のアプリケーションが '{op.current_app(ui)}' です")


# ================================================================ 測定時間の設定(設定画面)
# 時間の欄を押すと出るテンキー画面: ボタン群は1つの塊として検出されるので、塊の中の相対位置で押す
KEYPAD_COLS = (0.162, 0.5, 0.837)
KEYPAD_ROWS = (0.088, 0.269, 0.45, 0.631, 0.79, 0.932)
KEYPAD_POS = {"7": (0, 0), "8": (1, 0), "9": (2, 0), "4": (0, 1), "5": (1, 1), "6": (2, 1),
              "1": (0, 2), "2": (1, 2), "3": (2, 2), "0": (1, 3), ".": (2, 3), "クリア": (1, 4)}
KEYPAD_OK = (0.137, 0.932)


def fmt_phases(phases) -> str:
    return " + ".join(f"{p:g}" for p in phases)


def time_fields(ui: ScreenUI) -> list[UIElement]:
    """設定画面の時間入力欄(フェーズ順)。
    通常のモード: 各フェーズの [-] [+] ボタンの間 / Spectrometer Mode: 「期間」の右の欄"""
    small = sorted((b for b in ui.buttons if b.rect[2] < 120 and b.rect[3] < 120), key=lambda b: b.rect[1])
    rows: list[list[UIElement]] = []
    for b in small:
        if rows and abs(rows[-1][0].rect[1] - b.rect[1]) < 30:
            rows[-1].append(b)
        else:
            rows.append([b])
    fields = []
    for r in rows:
        if len(r) == 2:
            minus, plus = sorted(r, key=lambda b: b.rect[0])
            x0 = minus.rect[0] + minus.rect[2]
            fields.append(UIElement("field", "", (x0, minus.rect[1], plus.rect[0] - x0, minus.rect[3])))
    if not fields:
        lab = next((t for t in ui.texts if t.text.replace(" ", "") == "期間"), None)
        if lab:
            right = [t for t in ui.texts if t is not lab and t.rect[0] > lab.rect[0]
                     and abs(t.center[1] - lab.center[1]) < 30]
            if right:
                fields.append(min(right, key=lambda t: t.rect[0]))
    return fields


def _as_number(text: str) -> float | None:
    s = text.replace(" ", "").translate(str.maketrans("OoIl|", "00111"))
    try:
        return float(s)
    except ValueError:
        return None


def field_value(ui: ScreenUI, f: UIElement) -> float | None:
    """入力欄に表示されている数値(読めなければ None。欄の数字は screen_match が字形で読んで文字にしている)"""
    cx, cy = f.center
    for t in ui.texts:
        tx, ty = t.center
        if abs(tx - cx) <= f.rect[2] / 2 + 10 and abs(ty - cy) <= f.rect[3] / 2 + 10:
            v = _as_number(t.text)
            if v is not None:
                return v
    return None


def _keypad_tap(op: Operator, ui: ScreenUI, fx: float, fy: float):
    """テンキーのボタン群(1つの塊)の中の相対位置を押す"""
    x0, y0, w, h = max(ui.buttons, key=lambda b: b.rect[2] * b.rect[3]).rect
    op.click_elem(ui, UIElement("button", "", (int(x0 + fx * w) - 1, int(y0 + fy * h) - 1, 2, 2)), wait=0.25)


def _type_number(op: Operator, value: float) -> ScreenUI:
    """テンキー画面で クリア → 数字 → OK と押して、設定画面に戻るまで待つ"""
    ui = op.wait_state("keypad", 8)
    for key in ["クリア", *f"{value:g}"]:
        c, r = KEYPAD_POS[key]
        _keypad_tap(op, ui, KEYPAD_COLS[c], KEYPAD_ROWS[r])
    _keypad_tap(op, ui, *KEYPAD_OK)
    return op.wait_state("settings", 8)


def _close_settings(op: Operator, save: bool):
    ui = op.click_until("OK" if save else "キャンセル", ("main", "confirm"), 10, min_score=0.4 if save else 0.6)
    if op.state(ui) == "confirm":                  # 「現在の構成」画面が出たら OK
        op.click("OK", min_score=0.4)
        op.wait_state("main", 15)


DEFAULT_BOX_DX = 35          # 「デフォルト設定を使用」のチェック欄: 見つからないときは文字の左端からこれだけ左を押す


def checkbox_left_of(ui: ScreenUI, lab: UIElement) -> tuple[int, int]:
    """文字(lab)の左にあるチェック欄の中心(基準座標)。文字の高さの中央の行を左へたどり、欄の枠(暗い画素)の両端の真ん中。
    オンのときは文字に点線の枠が付いて OCR の位置が左へずれるので、決め打ちの距離では押しそこねる(2026-11-10 実機)"""
    x, y, w, h = lab.rect
    cy = y + h // 2
    img = ui.image.convert("L") if ui.image is not None else None
    if img is not None:
        dark = [cx for cx in range(max(0, x - 80), x) if img.getpixel((cx, cy)) < 110]
        if len(dark) >= 2 and dark[-1] - dark[0] >= 10:
            return (dark[0] + dark[-1]) // 2, cy
    return x - DEFAULT_BOX_DX, cy


def _turn_off_default(op: Operator, ui: ScreenUI) -> ScreenUI:
    """設定画面で「デフォルト設定を使用」がオン(時間の欄が無い)なら、チェック欄を押して外す。
    外せて時間の欄が出たら、その画面を返す(保存は呼び出し側の OK で行う)"""
    lab = next((t for t in ui.texts if "フォルト設定を使用" in t.text.replace(" ", "")), None)
    if lab is None:
        return ui
    say("  「デフォルト設定を使用」を外します")
    bx, by = checkbox_left_of(ui, lab)
    op.click_elem(ui, UIElement("button", "", (bx - 1, by - 1, 2, 2)), wait=1.0)
    for _ in range(READ_RETRY):
        ui = op.wait_state("settings", 8)
        if time_fields(ui):
            return ui
        _sleep(0.5)
    return ui


def set_phase_times(op: Operator, app: str, phases: list[float]) -> bool:
    """現在のアプリの設定画面を開き、各フェーズの時間を phases に合わせる(照射なし)。書き換えたら True。
    停止は照射中だけ押せるので、ここでは止まらない(2026-11-14 ユーザーの判断)"""
    to_main(op)
    ui = op.ui or op.read()
    b = ui.find_button("設定", 0.6) or max((b for b in ui.buttons if b.rect[1] < 500), key=lambda b: b.rect[0])
    op.click_elem(ui, b, wait=1.0)
    ui = op.wait_state("settings", 10)
    fields = time_fields(ui)
    turned_off = False
    if not fields:                                  # 「デフォルト設定を使用」がオン -> 外して時間の欄を出す
        ui = _turn_off_default(op, ui)
        fields = time_fields(ui)
        turned_off = bool(fields)
    if fields:
        phase_count_seen(app, len(fields))
    if len(fields) != len(phases):
        _close_settings(op, save=False)
        if not fields:
            raise DeviceError(f"{app}: 設定画面に時間の欄がありません(「デフォルト設定を使用」を外せませんでした)")
        raise DeviceError(f"{app}: 機器のフェーズ数は {len(fields)} 個、測定モードの設定は {len(phases)} 個です")
    changes = []
    for k, want in enumerate(phases):
        cur = field_value(ui, fields[k])
        if cur is not None and abs(cur - want) < 1e-6:
            continue
        for attempt in range(2):                    # 押しそこね・読み違いなら1回だけ入れ直す
            op.click_elem(ui, fields[k], wait=0.8)
            ui = _type_number(op, want)
            got = field_value(ui, time_fields(ui)[k])
            if got is not None and abs(got - want) < 1e-6:
                break
            if attempt == 0:
                say(f"  フェーズ{k + 1} を入れ直します(表示: {got})")
        else:
            _close_settings(op, save=False)
            raise OcrError(f"{app}: フェーズ{k + 1} の時間を {want:g} 秒にできませんでした(表示: {got})")
        before = "?" if cur is None else f"{cur:g}"
        changes.append(f"{before}→{want:g}")
    if turned_off:
        changes.insert(0, "デフォルト設定を使用 オフ")
    _close_settings(op, save=bool(changes))          # 外したときは、時間が同じでも OK で保存する
    if changes:
        say(f"  {app} の測定時間を設定: {', '.join(changes)} 秒")
    return bool(changes)


SWITCH_WAIT_S = 2      # 測定が終わってから次のモード切替(または終了処理)までの待ち。早すぎると機器がエラーになる
SAFETY_EXTRA_S = 600   # 安全上限: 設定時間を過ぎてもこの秒数以上測定中のままなら異常として中断


def measure(op: Operator, seconds: float) -> dict:
    """トリガーで測定開始 -> 機器が自動停止して結果画面になるまで確認を続ける。
    終了の判定は画面(結果画面になったか)で行い、秒数では判定しない。
    seconds(表の時間 = フェーズの合計)は、確認間隔・進捗表示・安全上限(+SAFETY_EXTRA_S)にだけ使う。
    戻り値の inst_time は結果画面に表示された機器の測定時間"""
    to_main(op)
    if CANCEL.is_set():
        raise Cancelled("ユーザーが停止しました")
    check_lid()                                    # 照射の直前(1 回ごと)に蓋が閉じているかを確かめる
    trig = op.trigger_state()
    if trig == "release":                          # 測定の前から引かれている = 手で操作中。こちらからは照射しない
        raise ManualTrigger("測定の前からトリガーが引かれています(手で操作中)")
    if trig != "pull":
        raise DeviceError(f"トリガーの状態が不明です(メニュー表示: {trig})")
    t0 = STEP["t0"] = time.time()                  # 照射したかもしれない(接続が切れたら、再開のとき機器の記録で確かめる)
    op.trigger()                                   # = F6 (Trigger Pull)
    say(f"  トリガー操作 -> {op.trigger_state()}")
    return _wait_result(op, seconds, t0)


def _wait_result(op: Operator, seconds: float, t0: float, first: bool = True) -> dict:
    """照射を始めた後(t0)、機器が自動停止して結果画面になるまで確認を続け、ファイル No と機器の時間を読む。
    first=False は接続が切れた後の再開(測定中の画面・結果画面から続ける)"""
    # ここから先は、どんな理由で中断しても(停止・時間超過・画面の異常・予期しない例外)トリガーを離す
    try:
        if first:
            op.wait_state(("measuring", "result"), 10)
        say(f"  照射中… {seconds:.0f}秒")
        # 結果画面になるまで確認を続ける(設定時間までは3秒間隔、過ぎたら1秒間隔)
        odd = 0                                    # 測定中・結果画面以外が続いた回数(1回だけなら読み違いとみなす)
        while True:
            el = time.time() - t0
            progress(el, seconds)
            if el > seconds + SAFETY_EXTRA_S:
                raise Overrun(f"設定時間を {SAFETY_EXTRA_S} 秒過ぎても測定が終わりません")
            _sleep(min(3, max(0.1, seconds - el)) if el < seconds else 1.0)
            st = op.state()
            if st == "result":
                break
            odd = 0 if st == "measuring" else odd + 1
            if odd >= 2:
                raise DeviceError(f"測定中に想定外の画面 '{st}' になりました")   # 機器側のエラー表示など(画面の読み違いではない)
    except BaseException:
        if op.trigger_state() == "release" or LID_TRIPPED.is_set():   # 引かれていれば離す(照射を止める。蓋の糸が先に送っていれば待つだけ)
            stop_irradiation(op)
            say("  トリガーを解除しました(Trigger Release)。照射停止を機器で確認してください")
        raise
    ui = op.ui
    if op.trigger_state() == "release":            # 自動停止後もトリガーが引かれたままなら離す
        stop_irradiation(op)
        say("  測定終了後、トリガーを解除しました")
    return {"elapsed": round(time.time() - t0, 1), **result_info(ui)}


RESULT_TABLE = (4, 182, 455, 478)   # 結果画面の元素の表の中(機器の px)。空なら測定が成り立っていない
EMPTY_INK = 0.005                    # 表の中の濃い点の割合がこれ未満なら空(ふつうの結果は 4〜5%、計数率が低すぎますは 0%。2026-11-13 実機)


def _native(ui):
    """読んだ画面を機器の大きさ(480x640)に戻す(画像が無ければ None)"""
    img = getattr(ui, "image", None) if ui is not None else None
    return img.resize((480, img.height * 480 // img.width)) if img is not None else None


def result_empty(ui: ScreenUI) -> bool:
    """結果画面の元素の表が空か(「計数率が低すぎます。」= 照射対象なし など。2026-11-13 実機: 題名の欄に知らせが出て表は空)"""
    img = getattr(ui, "image", None)
    if img is None:
        return False
    k = img.width / 480
    box = img.convert("L").crop(tuple(round(v * k) for v in RESULT_TABLE))
    hist = box.histogram()
    return sum(hist[:200]) / max(1, box.width * box.height) < EMPTY_INK


def result_info(ui: ScreenUI) -> dict:
    """結果画面からファイル No(sample_no)と機器の測定時間(inst_time)を読む。元素の表が空なら empty"""
    info = {"empty": True} if result_empty(ui) else {}
    from screen_match import recognized
    if not recognized(ui):                          # 結果画面のボタンはあるが、見本で読めない部分がある(知らせの窓が重なった など)
        info["odd"] = True
    if (frame := _native(ui)) is not None:
        import screen_match
        info["title"] = screen_match.match_title(frame, screen_match.title_samples())   # 見本に無ければ ""
    for t in ui.texts:
        s = t.text.replace(" ", "")
        if m := re.match(r"^(\d+)\d\d-\d\d", s):
            info["sample_no"] = m[1]
        elif s.startswith("時間") and (m := re.search(r"\d+(?:[.,]\d+)?", s[2:])):
            info["inst_time"] = m[0].replace(",", ".")    # 結果画面の機器の測定時間(「時間 30.0」「時間:30.0秒」など)
    return info


STOP_READ_S = 6.0      # 照射中に停止したとき、結果画面(途中までの測定)になるのを待って No を読む上限


def stopped_result(op: Operator, wait: float = STOP_READ_S) -> dict:
    """照射中に停止した後(トリガーは離してある): 機器が途中までの測定を結果画面に出すのを待ち、No と時間を読む。
    読めなければ {}。停止の要求が出ているので _sleep は使わない"""
    for _ in range(max(1, int(wait / 0.5))):        # 回数で区切る(読み取りに時間がかかっても、時計が止まっていても必ず終わる)
        try:
            ui = op.read()
            if op.state(ui) == "result":
                return result_info(ui)
        except Exception:
            pass
        time.sleep(0.5)
    return {}


# ================================================================ 接続が切れたときの再開
RESUME_WAIT_S = 600    # 測定の途中で接続が切れたとき、つなぎ直しを待つ上限(本体画面の「接続」で、同じ機器へつなぎ直すのを待つ)
RESUME_MAX = 3         # 1回の測定でつなぎ直して再開する上限
RESULTS_RETRY = 5      # 機器の結果表に新しい行が現れるまで読み直す回数(1 秒おき)
STEP = {"t0": None, "app": ""}    # 今の回で照射を始めた時刻(measure がトリガーの前に入れる。None = まだ照射していない)とモード
LOGIN_EVENT = threading.Event()   # 再開を待つ間の「ログイン」(GUI が request_login で知らせる)
LOGIN_CODE = [""]


def request_login(code: str):
    """「ログイン」が押された(ログイン画面で再開を待っているとき): その PASS でログインして再開する"""
    LOGIN_CODE[0] = code
    LOGIN_EVENT.set()


def wait_reconnect(op: Operator, on_wait, kind: str = "offline"):
    """接続が切れた: 本体画面がつなぎ直すのを待ち、つながった接続に付け替える(RESUME_WAIT_S まで。停止なら Cancelled)"""
    relink = getattr(op.rc, "relink", None)
    if relink is None:
        raise ConnectionError("接続が切れました(つなぎ直せない相手なので再開しない)")
    on_wait(kind)                                  # offline = 続きから再開 / cut = 照射の途中で切れた(結果を読んで終える)
    end = time.time() + RESUME_WAIT_S
    while not relink():
        if time.time() > end:
            raise ConnectionError(f"{RESUME_WAIT_S / 60:g} 分待ってもつながりませんでした")
        _sleep(0.5)
    op.ui = None
    say("  機器につなぎ直しました。続きから再開します")
    on_wait("resumed")


def ensure_ready(op: Operator, on_wait):
    """再開の前: ログイン画面・起動画面なら「ログイン」ボタンを待ってログインし(ログインはこのボタンからだけ。2026-11-12 ユーザーの判断)、
    初期化中・放射線の注意なら手で進めるのを待つ(数秒おきに読み直す)。ほかの画面になったら戻る"""
    told = None
    while True:
        st = op.state()
        if st in ("login", "startup"):
            if told != "login":
                LOGIN_EVENT.clear()                    # 前に押した分は使わない
                on_wait("login")
                told = "login"
            if not LOGIN_EVENT.wait(0.5):
                if CANCEL.is_set():
                    raise Cancelled("ユーザーが停止しました")
                continue
            LOGIN_EVENT.clear()
            try:
                if st == "startup":
                    open_login(op)
                login(op, LOGIN_CODE[0])
            except (LoginWait, LoginFailed, OcrError, DeviceError) as e:
                say(f"  ログインできませんでした({e})。「ログイン」でもう一度")
                on_wait("login_failed")
            continue
        if st in ("booting", "safety"):
            if told != st:
                on_wait(st)
                told = st
            _sleep(3.0)
            continue
        if told:
            on_wait("resumed")
        return


def _same_app(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def resume_step(op: Operator, app: str, seconds: float, t0: float, results_after, last_no) -> dict | None:
    """照射を始めた後に接続が切れた回を、つなぎ直した後に決める。戻り値: 結果(measure と同じ形)/ None(照射されていない → やり直す)
      - 測定中・結果画面: そのまま続ける(結果画面から No と時間を読む)
      - それ以外(ログイン画面など。結果画面が消えた): 機器の結果表で、前の No より後の行を探す。1行でアプリ名が同じなら
        その回の結果(No = File #、時間 = ElapsedTime)。無ければ照射されていない。2行以上・アプリ名が違えば機器エラー"""
    st = op.state()
    if st in ("measuring", "result"):
        say(f"  {'測定の続きを待ちます' if st == 'measuring' else '測定は終わっていました'}")
        return _wait_result(op, seconds, t0, first=False)
    if op.trigger_state() == "release":            # 測定中でないのに引かれたまま: 離す
        stop_irradiation(op)
    if results_after is None or last_no is None:
        raise DeviceError(f"接続が切れている間に結果画面が消えました(現在: {st})。機器の結果表を読めないので、この回の結果を確かめられません")
    rows = []
    for i in range(RESULTS_RETRY):
        rows = results_after(last_no)
        if rows:
            break
        if i < RESULTS_RETRY - 1:
            _sleep(1.0)
    if not rows:
        say("  機器の記録に新しい測定が無いので、照射されていません。この回をやり直します")
        return None
    if len(rows) > 1 or not _same_app(rows[0].get("Application", ""), app):
        raise DeviceError(f"接続が切れている間の測定を確かめられません(機器の結果表に新しい行 {len(rows)} 件: "
                          + ", ".join(f"No.{r.get('File #', '?')} {r.get('Application', '?')}" for r in rows) + ")")
    r = rows[0]
    say(f"  接続が切れている間に測定が終わっていました(機器の記録: No.{r.get('File #', '').strip()})")
    return {"elapsed": round(time.time() - t0, 1), "sample_no": r.get("File #", "").strip(),
            "inst_time": r.get("ElapsedTime", "").strip(), "recovered": True}


def log(row: dict):
    ensure_parent(LOG_CSV)
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="", encoding="utf-8-sig") as f:
        # "cycle" 列は旧版の名残(今は空)。既存の measure_log.csv と列をそろえるため残している
        w = csv.DictWriter(f, ["time", "cycle", "app", "set_s", "elapsed", "inst_time", "sample_no", "status"])
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in w.fieldnames})


OVERRUN_S = 15   # 機器の測定時間が読めないとき: 表の時間よりこれ以上長くかかったら「ずれ」とみなす(通常の遅れは 4〜7 秒)


def time_mismatch(row: dict) -> str | None:
    """結果画面の機器の測定時間(「時間 30.0」)と表の時間がずれていれば、その説明を返す"""
    sec = row["set_s"]
    try:
        inst = float(str(row.get("inst_time", "")).replace(",", "."))
    except ValueError:
        inst = None
    if inst is not None:
        return None if abs(inst - sec) < 0.5 else f"設定 {sec:g} 秒 / 機器 {inst:g} 秒"
    over = row.get("elapsed", 0) - sec
    return f"設定 {sec:g} 秒 / 機器不明" if over > OVERRUN_S else None


def run_plan(op: Operator, plan: list[tuple[str, list[float]]], set_each: bool = True,
             on_step=lambda i, n, app, sec: None, on_result=lambda row: None, on_stage=lambda stage: None,
             on_wait=lambda kind: None, results_after=None, last_no: int | None = None):
    """計画を上から順に1回ずつ実行。使えない回(failure)は row["failed"] に理由を入れて on_result で知らせ、次の回へ進む
    (測り直しは、ターンの後に GUI の「再計測」で。2026-11-16 ユーザーの判断)。plan は [(アプリ名, [フェーズごとの秒数]), ...]。待機時間はフェーズの合計。
    同じモードを繰り返したいときは plan に複数回並べる。
      set_each=True : 各測定の前に、その測定時間を機器の設定画面へ書き込む(フェーズ数が合わなければ照射せずに中断)
      set_each=False: 機器の設定は変えず、表の時間を目安に待つ。機器の時間とずれていたら終了まで待ち、
                      row["mismatch"] に説明を入れて知らせる
    on_stage は各測定の段階("切替中" / "設定中" / "照射中")を知らせる。
    接続が切れたら(ConnectionError・TimeoutError)、中断せずに本体画面のつなぎ直しを待って続きから再開する
    (on_wait("offline" / "login" / "login_failed" / "booting" / "safety" / "resumed") で知らせる。wait_reconnect・resume_step・
    ensure_ready)。照射を始めた後に切れて結果画面が消えていたら、results_after(前の No) が返す機器の結果表の行で決める
    (last_no = 開始のときの最後の No。測定が終わるたびに進める)。
    途中で蓋が開いたら(lid_opened)、照射中はトリガーを離して LidOpen で止める(その回は失敗「機器エラー」)。
    AbortError/Cancelled/TimeoutError は呼び出し側で処理する"""
    n = len(plan)
    resumed = False                                # 一度でもつなぎ直したか(ログイン画面などの確認をする)
    global _OP
    LID_TRIPPED.clear()
    check_lid()
    _OP = op
    RUNNING.set()
    try:
        if LID_OK is not None and not LID_OK():    # 確かめた直後に開いた(lid_opened は RUNNING の前で知らせが来ない)
            lid_opened()
        for i, (app, phases) in enumerate(plan, 1):
            row = _run_step(op, i, n, app, phases, set_each, on_step, on_result, on_stage, on_wait,
                            results_after, last_no, resumed)
            resumed = resumed or row.pop("_resumed", False)
            if str(row.get("sample_no", "")).isdigit():
                last_no = int(row["sample_no"])
        _sleep(SWITCH_WAIT_S)
        if resumed:
            ensure_ready(op, on_wait)
        to_main(op)
    except Cancelled as e:
        if LID_TRIPPED.is_set():                   # 停止ではなく蓋が開いた
            raise LidOpen("測定の途中で蓋が開きました") from e
        raise
    finally:
        RUNNING.clear()
        _OP = None
    say("全測定完了")


# 失敗の理由の分類(2026-11-13 ユーザーの判断)。接続エラーだけ後ろに「機器の測定時間/設定の時間 秒」を付ける(読めなければ付けない)
FAIL_CUT = "接続エラー"     # 照射の途中で接続が切れた
FAIL_STOP = "停止エラー"    # 時間の前に照射が止まった: 停止を押した・機器の測定時間が設定より短い(本体で止めた など)。時間は出さない
FAIL_DEVICE = "機器エラー"   # 結果画面が正しくない(表が空 = 計数率が低すぎます など・見本で読めない部分がある)・照射の後の思わぬ画面などで中断した


def failure(row: dict, set_each: bool, cause: str = "") -> str:
    """使えない回なら理由を返す。cause は照射が止まった理由: "" = ふつうに終わった / "cut" = 照射の途中で接続が切れた /
    "stop" = 照射中に「停止」を押した / "lid" = 照射中に蓋が開いた(機器エラー)/ "device" = 照射の後に思わぬ画面・時間超過で中断した。
    接続が切れて・停止して、機器の測定時間が設定より短い・読めない(接続エラー / 停止エラー。時間が設定どおりなら照射は
    終わっていたので、ふつうの回と同じ)/ 中断した(機器エラー)/ 結果画面の元素の表が空・読めない部分がある・題名がモードの
    名前でない(機器エラー)/ 時間を毎回書き込んでいるのに機器の測定時間が設定より短い(停止エラー。本体で止めたなど)。
    時間を書き込まないときの短さは、機器の設定が違うだけなので失敗にしない(計測時間誤差のアラートだけ)"""
    if cause == "device":
        return FAIL_DEVICE                          # 測定の処理が中断した: 結果画面があっても使わない
    sec = row["set_s"]
    try:
        inst = float(str(row.get("inst_time", "")).replace(",", "."))
    except ValueError:
        inst = None
    if cause in ("cut", "stop", "lid") and (inst is None or inst < sec - 0.5):   # 時間の前に照射が止まった
        return {"cut": FAIL_CUT, "stop": FAIL_STOP, "lid": FAIL_DEVICE}[cause]   # 台帳の理由に秒数は出さない(2026-11-14)。蓋は機器エラー(2026-11-20)
    if row.get("empty") or row.get("odd"):
        return FAIL_DEVICE                          # 結果画面の表が空(計数率が低すぎます など)・読めない部分がある
    if "title" in row and row.get("title_known") and not _same_app(row["title"], row["app"]):
        return FAIL_DEVICE                          # 題名がモードの名前でない(知らせの文・別のモード)
    if set_each and inst is not None and inst < sec - 0.5:
        return FAIL_STOP                            # 時間の前に照射が止まった(本体で止めた など)
    return ""


def _judge(row: dict, app: str, set_each: bool, cause: str = ""):
    """照射した回を判定する(止まった理由 cause によらず同じ決まり。failure): 題名・時間のずれ・失敗の理由を row に入れる"""
    if "title" in row:                             # 題名を確かめられるのは、そのモードの見本があるときだけ
        import screen_match
        row["title_known"] = app in screen_match.title_samples()
    reason = failure(row, set_each, cause)
    if not reason and (msg := time_mismatch(row)):   # 失敗の回は時間のずれを別に知らせない
        row["mismatch"] = msg
        say(f"  [ずれ] {app}: {msg}")
    if reason:
        row.update(failed=reason, status="failed")
        say(f"  [失敗] {app}: {reason}")


def _interrupted(op, row: dict, app: str, set_each: bool, cause: str, on_result):
    """照射を始めた後に止まった回(停止・思わぬ画面・時間超過・つなぎ直せない接続切れ): 機器にデータが残るので、結果画面を読み
    (接続切れは読めない)、ふつうの回と同じ決まりで判定して知らせる(台帳に載せる。2026-11-13 ユーザーの判断)"""
    info = {} if cause == "cut" else stopped_result(op)
    row.update(info, elapsed=round(time.time() - STEP["t0"], 1), status="ok")
    _judge(row, app, set_each, cause)
    say(f"  {'停止' if cause == 'stop' else '蓋が開いて停止' if cause == 'lid' else '中断'} No.{row.get('sample_no') or '?'}({row.get('failed') or '有効'})")
    on_result({k: v for k, v in row.items() if k != "_resumed"})


def _run_step(op, i, n, app, phases, set_each, on_step, on_result, on_stage, on_wait,
              results_after, last_no, resumed) -> dict:
    """1回の測定(切れたら再開を待つ)。結果の行を返す(失敗なら row["failed"] に理由。つなぎ直したら row["_resumed"])"""
    sec = sum(phases)
    if i > 1:
        _sleep(SWITCH_WAIT_S)                      # 前の測定の直後に切り替えない(機器のエラー対策)
    on_step(i, n, app, sec)
    say(f"[{i}/{n}] {app} {fmt_phases(phases)}秒")
    row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "set_s": sec}
    STEP["t0"], STEP["app"] = None, app
    tries, cut = 0, False
    try:
        info = None
        while info is None:
            try:
                if STEP["t0"] is None:         # まだ照射していない(はじめから・やり直し)
                    if resumed:
                        ensure_ready(op, on_wait)
                    on_stage("切替中")
                    switch_app(op, app)
                    if set_each:
                        on_stage("設定中")
                        set_phase_times(op, app, phases)
                    on_stage("照射中")
                    info = measure(op, sec)
                else:                          # 照射を始めた後に切れた: どうなったかを決める(照射の途中で切れたら結果を読むだけ)
                    on_stage("結果確認中" if cut else "照射中")
                    info = resume_step(op, app, sec, STEP["t0"], results_after, last_no)
                    if info is None:
                        STEP["t0"] = None      # 照射されていない: はじめからやり直す
            except (ConnectionError, TimeoutError) as e:
                if tries >= RESUME_MAX:
                    raise
                tries += 1
                cut = cut or STEP["t0"] is not None    # 照射の途中で切れた(USB が切れると機器は照射を止める)
                say(f"  接続が切れました({e})。" + ("照射は止まりました。つなぎ直したら結果を読んで終えます" if cut
                                                   else "つなぎ直しを待ちます"))
                wait_reconnect(op, on_wait, "cut" if cut else "offline")
                resumed = row["_resumed"] = True
        row.update(info, status="ok")
        _judge(row, app, set_each, "cut" if cut else "")
        say(f"  完了 No.{row.get('sample_no', '?')} ({row['elapsed']}秒)")
        on_result({k: v for k, v in row.items() if k != "_resumed"})
        if row.get("failed") == FAIL_CUT:              # 照射の途中で切れた: 停止と同じ扱いでターンを終える(測り直しは聞かない。
            raise CutStop(f"{app}: 照射の途中で接続が切れ、照射が止まりました")   # 未了は「再計測」で。2026-11-14 ユーザーの判断)
    except CutStop:
        raise
    except Exception as e:                         # 照射を始めた後に止まった(まだ知らせていない回): 止まった理由を付けて判定する
        if STEP["t0"] is not None and "status" not in row:
            _interrupted(op, row, app, set_each,
                         ("lid" if LID_TRIPPED.is_set() else "stop") if isinstance(e, Cancelled) else
                         "cut" if isinstance(e, (ConnectionError, TimeoutError)) else "device", on_result)
        row.setdefault("status", "cancelled" if isinstance(e, Cancelled) else "error")
        raise
    finally:
        row.setdefault("status", "error")
        log(row)
    return row


# ================================================================ main
def parse_plan(s: str) -> list[tuple[str, list[float]]]:
    """"A=30,B=15+60" -> [("A", [30.0]), ("B", [15.0, 60.0])]"""
    plan = []
    for item in s.split(","):
        name, secs = item.rsplit("=", 1)
        phases = [float(x) for x in secs.split("+")]
        if any(p != int(p) or p < 1 for p in phases):      # 機器の設定画面は整数秒のみ
            raise ValueError(f"{name.strip()}: 測定時間は1秒以上の整数で指定してください({secs})")
        plan.append((name.strip(), phases))
    return plan


def main():
    a = sys.argv[1:]
    opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    op = Operator()

    if "--check" in a:
        ui = op.read()
        print(f"状態: {op.state(ui)}  アプリ: {op.current_app(ui)!r}")
        print("ボタン:", [b.text for b in ui.buttons])
        print("テキスト:", [t.text for t in ui.texts])
        return 0

    if "--login" in a:          # GUI に保存した PASS で自動ログインだけ行う(照射なし)
        global PASSCODE
        PASSCODE = load_settings(SETTINGS).get("pass", "")
        if op.state() != "login":
            print("ログイン画面ではありません:", op.state()); return 0
        login(op, PASSCODE)
        print("ログイン完了:", op.state())
        return 0

    if "--switch-only" in a:
        switch_app(op, opt("--switch-only"))
        print("切替完了:", op.current_app(op.read()))
        return 0

    if "--set-times" in a:      # 測定時間を機器に書き込むだけ(照射なし)
        for app, phases in parse_plan(opt("--set-times")):
            switch_app(op, app)
            changed = set_phase_times(op, app, phases)
            print(f"{app}: {fmt_phases(phases)} 秒", "(書き込み)" if changed else "(変更なし)")
        return 0

    plan = parse_plan(opt("--plan"))
    global LID_OK                    # 照射は蓋が閉じているときだけ(GUI と同じ。LID_SENSOR.md)
    import lid_sensor
    lid = lid_sensor.start()
    time.sleep(lid_sensor.PRESENT_S + 0.5)          # 最初の確かめを待つ
    LID_OK = lid.closed
    lid.add_listener(lambda st: st != lid_sensor.CLOSED and lid_opened())
    if not lid.closed():
        print("蓋が開いているか、蓋のセンサーが見つかりません。照射しません"); return 1
    total = sum(sum(p) for _, p in plan)
    print(f"計画: {plan}  (照射合計 約{total/60:.1f}分)")
    if "--yes" not in a and input("X線照射を行います。試料と周囲の安全を確認しましたか? [y/N] ").strip().lower() != "y":
        print("中止しました"); return 1

    try:
        run_plan(op, plan, set_each="--no-set" not in a)
        return 0
    except (AbortError, TimeoutError) as e:
        op.save_error()
        print(f"[中断] {e}\n  画面を {ERROR_PNG} に保存しました")
        return 2
    except KeyboardInterrupt:
        op.save_error()
        print("[中断] Ctrl+C。測定中の場合は機器側で照射が止まっているか確認してください")
        return 3


if __name__ == "__main__":
    use_base()
    sys.exit(main())
