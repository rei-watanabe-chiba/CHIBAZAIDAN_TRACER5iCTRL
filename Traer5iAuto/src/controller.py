"""測定の進め方(画面を持たない。web_app が使う。2026-11-19〜。決まりは前の Tk 版の measure_gui.App を書き写した)

機器の状態の読み取り・状態の欄とメインのボタン・入力の不備・台帳との組み合わせ(再計測 / 再開)・照射前の確認・測定の実行・
停止(照射中だけ)・再開を待つ・セットの終わり方・取り込み・閉じるときの後始末。
アプリの状態を変えるのは いつも 1 本の糸(WebView2 版は「画面の糸」)。裏の糸からは post で渡す。
画面の側(view)は映すだけ(下の View の説明)。測定モード表(Modes)と台帳(LedgerModel)の中身もここに持つ
"""
from __future__ import annotations

import csv, os, threading, time, traceback

import app_rules as rules
import auto_measure as am
import device_link as dl
import lid_sensor
from gui_config import CONTINUE_MODES, READY_STATES, START_MODES, UI
from instrument_files import RESULTS, fetch_measurements, file_no, results_rows
from ledger_core import LedgerBook, keys_of
from refs_core import RefBook
from modes_core import PHASE_KEYS, default_times, issues as mode_issues, parse_phases, phase_count, phases_bad, saved_rows, total_text
from shutdown import release_trigger
from storage import append_log


def keep_screen(op):
    """今の機器の画面を config/new_screens/ に残す(調べる用。同じ画面は1枚・上限あり)"""
    try:
        link = getattr(op.rc, "link", None)
        if link is not None:
            dl._keep_new_screen(link.snapshot())
    except Exception:
        pass


class Modes:
    """測定モード表の中身(上から順に1回ずつ測る)。行 = {id, app, p: [欄の値 × MAX_PHASES], draft: 表へ入れていない欄の値 | None}"""

    def __init__(self, apps: list[str], phase_counts: dict, on_change, on_check, show):
        self.apps, self.phase_counts = apps, phase_counts
        self.on_change, self.on_check, self.show = on_change, on_check, show   # show(): 画面へ映す
        self.rows: list[dict] = []
        self.row_state: dict[str, str] = {}
        self._locked = False
        self._seq = 0

    @property
    def locked(self) -> bool:
        return self._locked

    @locked.setter
    def locked(self, on: bool):
        self._locked = on
        self.show()

    def ids(self) -> list[str]:
        return [r["id"] for r in self.rows]

    def row(self, rid) -> dict | None:
        return next((r for r in self.rows if r["id"] == rid), None)

    def modes_of(self, items=None) -> list[str]:
        return [self.row(i)["app"] for i in (self.ids() if items is None else items)]

    def phases(self, rid) -> list[float]:
        return [float(v) for v in self.row(rid)["p"] if v]

    def plan(self) -> list[tuple[str, list[float]]]:
        return [(r["app"], self.phases(r["id"])) for r in self.rows]

    def count(self, rid) -> int:
        r = self.row(rid)
        return phase_count(r["app"], len([v for v in r["p"] if v]), self.phase_counts)

    def _new(self, app: str, phases: list[str]) -> dict:
        self._seq += 1
        p = (list(phases) + [""] * len(PHASE_KEYS))[:len(PHASE_KEYS)]
        return {"id": f"m{self._seq}", "app": app, "p": p, "draft": None}

    def load(self, saved: list):
        self.rows = [self._new(app, phases) for app, phases in saved_rows(saved)]
        self.show()

    def bad(self, rid) -> bool:
        r = self.row(rid)
        if r["draft"] is not None:
            return phases_bad(r["draft"])
        n = self.count(rid)
        return phases_bad({k: r["p"][i] for i, k in enumerate(PHASE_KEYS[:n])})

    def issues(self) -> list[str]:
        return mode_issues([self.bad(i) for i in self.ids()])

    def learn_phase_count(self, app: str, n: int) -> bool:
        if self.phase_counts.get(app) == n:
            return False
        self.phase_counts[app] = n
        self.show()
        return True

    def _changed(self):
        self.row_state.clear()
        self.show()
        self.on_change()
        self.on_check()

    # ---- 画面からの操作(測定中は受け付けない)
    def add(self, after: str | None = None) -> str | None:
        """「＋ 追加」: 選んだ行の下(無ければ最後)に、同じモード(無ければ最初のモード)の行を初期値の秒数で"""
        if self._locked or not self.apps:
            return None
        ref = self.row(after) if after else (self.rows[-1] if self.rows else None)
        app = ref["app"] if ref else self.apps[0]
        new = self._new(app, default_times(app, self.phase_counts))
        idx = self.rows.index(self.row(after)) + 1 if after and self.row(after) else len(self.rows)
        self.rows.insert(idx, new)
        self._changed()
        return new["id"]

    def delete(self, ids) -> bool:
        if self._locked or not ids:
            return False
        self.rows = [r for r in self.rows if r["id"] not in set(ids)]
        self._changed()
        return True

    def set_mode(self, rid, app: str) -> bool:
        """行のモードを変える(秒数は新しいモードの初期値)"""
        r = self.row(rid)
        if self._locked or r is None or app not in self.apps:
            return False
        r["app"], r["p"], r["draft"] = app, default_times(app, self.phase_counts), None
        self._changed()
        return True

    def set_phases(self, rid, values: list[str], commit: bool = True) -> bool:
        """フェーズの欄の値(出している欄の数だけ)。正しければ表へ入れる。不正なら表に入れず欄の値として残す(draft)。
        commit=False(打っている途中)は検査だけ。表へ入れたら True"""
        r = self.row(rid)
        if self._locked or r is None:
            return False
        raw = {k: str(v) for k, v in zip(PHASE_KEYS, values)}
        try:
            out = parse_phases(raw)
        except ValueError:
            r["draft"] = raw
            self.show(); self.on_check()
            return False
        if not commit:
            r["draft"] = raw if any(raw[k] != r["p"][i] for i, k in enumerate(raw)) else None
            self.show(); self.on_check()
            return False
        r["p"] = [out.get(k, "") for k in PHASE_KEYS]
        r["draft"] = None
        self._changed()
        return True

    def revert(self, rid):
        """Esc: 欄を表の値に戻す"""
        r = self.row(rid)
        if r is not None and r["draft"] is not None:
            r["draft"] = None
            self.show(); self.on_check()

    def show_states(self, states: dict):
        if states != self.row_state:
            self.row_state = dict(states)
            self.show()

    def payload(self) -> dict:
        """画面へ渡す表の中身"""
        rows = []
        for n, r in enumerate(self.rows, 1):
            st = self.row_state.get(r["id"])
            k = self.count(r["id"])
            vals = [r["draft"].get(key, "") for key in PHASE_KEYS] if r["draft"] is not None else list(r["p"])
            bad = [phases_bad({key: vals[i]}) for i, key in enumerate(PHASE_KEYS[:k])] if r["draft"] is not None else [False] * k
            rows.append({"id": r["id"], "no": UI.MODE_ROW_STATE[st] if st else str(n), "tag": st if st in UI.MODE_ROW_COLORS else "",
                         "app": r["app"], "phases": vals[:k], "bad": bad})
        return {"rows": rows, "apps": self.apps, "locked": self._locked, "total": total_text(self.plan())}


class LedgerModel:
    """台帳の中身(LedgerBook)と計測ファイルの欄の値。画面の台帳(3 段目)はこれを映す。
    on_rows(): 台帳の行が変わった / on_lock(on, values): 欄を変えられるか / alert(タイトル, 説明)"""

    def __init__(self, folder: str, store: str, alert, on_rows=lambda: None, on_lock=lambda on, values: None):
        self.folder, self.on_rows, self.on_lock = folder, on_rows, on_lock
        self.book = LedgerBook(folder, store, on_error=lambda kind, err: alert(*rules.alert_parts(kind, err=err)))
        self.book.load()
        self.values = {key: "" for key, *_ in UI.INFO_FIELDS}
        self.locked = False
        self.saved = True                             # 最後の保存ができたか(できなければ「⚠未保存」。次の変更で書き直す)
        self.on_values = lambda: None                 # 欄の値が変わった(状態の欄の右・入力の検査)
        self.on_view = lambda: None                   # 台帳の表を映し直す(画面の側)
        self.log = lambda msg: None

    keys_of = staticmethod(keys_of)

    def current_values(self) -> dict:
        return {k: str(v).strip() for k, v in self.values.items()}

    def set_values(self, values: dict):
        changed = False
        for k, v in (values or {}).items():
            if k in self.values and self.values[k] != v:
                self.values[k] = v; changed = True
        if changed:
            self.on_values()

    def save(self):
        self.saved = self.book.save()
        self.on_view()
        self.on_rows()

    def lock_inputs(self, on: bool):
        self.locked = on
        self.on_lock(on, self.current_values())
        self.book.mark()
        self.on_view()                                # 「セット計測中」の行の印と色

    # ---- 画面の台帳(セルの編集・有効 / 失敗・削除。終わっていないセットの行は受け付けない)
    def payload(self) -> dict:
        """画面へ渡す台帳: 列 [(名前, 幅, 編集できるか)]・行 [{id, cells, tag, locked}]・保存先・保存できたか"""
        live = self.book.live_ids()
        rows = []
        for rid, r in self.book.rows.items():
            state, turn, tag = self.book.shown(rid, live)
            cells = [state if c == UI.LEDGER_STATE_COL else turn if c == UI.LEDGER_TURN_COL else r.get(c, "")
                     for c, _e in UI.LEDGER_COLS]
            rows.append({"id": rid, "cells": cells, "tag": tag, "locked": r["turn"] is not None and r["turn"] in live})
        return {"cols": UI.LEDGER_COLS, "rows": rows, "folder": os.path.abspath(self.folder), "saved": self.saved}

    def set_cell(self, rid, col: str, value: str) -> bool:
        editable = next((e for c, e in UI.LEDGER_COLS if c == col), False)
        if not editable or rid not in self.book.rows or self.book.locked([rid]):
            return False
        if self.book.set_cell(rid, col, str(value).strip()):
            self.save()
            return True
        return False

    def set_failed(self, rids, reason: str) -> bool:
        rids = [r for r in rids if r in self.book.rows]
        if self.book.set_failed(rids, reason):
            self.save()
            return True
        return False

    def delete(self, rids) -> bool:
        rids = [r for r in rids if r in self.book.rows]
        if self.book.delete(rids):
            self.save()
            self.log(f"[台帳] {len(rids)} 行を削除しました")
            return True
        return False

    def finish(self):
        if self.book.dirty:
            self.save()

    # ---- セット・行(LedgerBook に渡して保存する)
    def begin_turn(self, values, modes) -> int:
        tid = self.book.begin_turn(values, modes); self.save(); return tid

    def open_turn(self, values):
        return self.book.open_turn(values)

    def live_turn(self):
        return self.book.live_turn()

    def close_turn(self, tid):
        if self.book.close_turn(tid):
            self.save()

    def abort_turn(self, tid) -> int:
        n = self.book.abort_turn(tid); self.save(); return n

    def drop_turn(self, tid):
        if self.book.drop_turn(tid):
            self.save()

    def turn_done(self, tid):
        return self.book.turn_done(tid)

    def _turn(self, tid):
        return self.book.turn(tid)

    def pair(self, tid, modes):
        return self.book.pair(tid, modes)

    def measured(self, values, modes) -> bool:
        return self.book.measured(values, modes)

    def turn_warned(self, values) -> bool:
        return self.book.turn_warned(values)

    def add_measurement(self, no: str, mode: str, failed: str = "", turn: int | None = None):
        self.book.add_measurement(no, mode, self.current_values(), failed, turn)
        self.save()


class RefsModel:
    """資料一覧の中身(RefBook)と検索の状態(ON/OFF・対象・文字列)。画面の側は届いた行を、見えている所だけ描く。
    show_data(中身): CSV の中身(列・行・行番号)を画面へ / show_view(中身): 出す行の番号・並べ替え・割り当て・説明を画面へ /
    use(値): 行のクリックで、割り当てた列の値を計測ファイルの欄へ / alert(タイトル, 説明) / on_roles(): 割り当てが変わった"""

    def __init__(self, show_data, show_view, use, alert, on_roles=lambda: None):
        self.data = RefBook()
        self.show_data, self.show_view, self.use_values, self.alert, self.on_roles = show_data, show_view, use, alert, on_roles
        self.on, self.target, self.query = False, UI.REF_ALL, ""
        self.file_label = UI.REF_NO_FILE
        self.enabled = True                           # セット計測中は行のクリックで欄へ入れない

    def load(self, path: str, roles: dict | None = None, join=None) -> bool:
        try:
            self.data.load(path, roles, join)
        except (OSError, ValueError, csv.Error) as e:
            self.alert(*rules.alert_parts("refs_read", file=os.path.basename(path), err=e))
            self.file_label = f"{os.path.basename(path)}  ⚠読めません"
            self.send_view()
            return False
        self.file_label = os.path.basename(path)
        if self.target not in self.data.cols:
            self.target = UI.REF_ALL
        self.send_data()
        self.on_roles()
        return True

    def restore(self, s: dict | None):
        s = s or {}
        if s.get("path"):
            if os.path.exists(s["path"]):
                roles = s.get("roles")
                if roles is None and s.get("col"):    # 前の版: ID の列だけ
                    roles = {"ID": s["col"]}
                self.target = s.get("target") or UI.REF_ALL
                self.load(s["path"], roles, s.get("join"))
                return
            self.data.path = s["path"]
            self.file_label = f"{os.path.basename(s['path'])}  ⚠見つかりません"
        self.send_view()

    def settings(self) -> dict:
        d = self.data
        return {"path": d.path, "roles": dict(d.roles), "join": list(d.join) if d.join else None, "target": self.target}

    def send_data(self):
        d = self.data
        self.show_data({"cols": d.cols, "rows": d.rows, "lines": d.lines, "join": d.join_name, "head": d.head})
        self.send_view()

    def send_view(self):
        d = self.data
        sort = d.sort
        self.show_view({"view": d.view(self.query if self.on else "", self.target) if d.head else [],
                        "sort": [sort[0], sort[1]] if sort else None, "roles": dict(d.roles),
                        "on": self.on, "target": self.target, "query": self.query, "file": self.file_label,
                        "join": list(d.join) if d.join else None, "join_name": d.join_name, "enabled": self.enabled})

    # ---- 画面からの操作
    def search(self, on: bool, target: str, query: str):
        """検索: 文字列を打つと ON にする(OFF なら全件。入力は残す)"""
        if query.strip() and not self.on and query != self.query:
            on = True
        self.on, self.target, self.query = bool(on), target if target in self.data.cols else UI.REF_ALL, query
        self.send_view()

    def sort_by(self, col: str):
        self.data.sort_by(col)
        self.send_view()

    def assign(self, col: str, role: str | None):
        if col in self.data.cols and (role is None or role in UI.REF_ROLES):
            self.data.assign(col, role)
            self.send_view()
            self.on_roles()

    def make_join(self, a: str, sep: str, b: str) -> bool:
        if not self.data.can_join(a, sep, b):
            return False
        self._joined(self.data.set_join((a, sep, b)))
        return True

    def drop_join(self):
        if self.data.join:
            self._joined(self.data.set_join(None))

    def _joined(self, names):
        old, new = names
        if self.target == old:
            self.target = new or UI.REF_ALL
        self.send_data()
        self.on_roles()

    def set_enabled(self, on: bool):
        self.enabled = on
        self.send_view()

    def use(self, n: int) -> bool:
        """行 n(CSV の何行目か。0 から)の割り当てた列の値を計測ファイルの欄へ(セット計測中は入れない)"""
        if not self.enabled or not 0 <= n < len(self.data.rows):
            return False
        self.use_values(self.data.values(n))
        return True


class Controller:
    """測定の進め方。view(画面の側)は次を持つ:
      status(タイトル, (地, 字) | None)・detail(文)・aside({Database, ID})・progress(出すか, 0〜1)
      button(文字, 色, 押せるか)・end_button(出すか, 押せるか)・modes(表の中身)・lock_info(変えられないか, 値)
      confirm(タイトル, 文, チェック, 照射開始の文字) → 答えは confirm_answer(ok, チェック)・message(タイトル, 文)
      log(文)・alert(タイトル, 説明)・select_log()・manual(手で押せるか)・lock_settings(変えられないか)
      ledger(台帳の中身)・refs_data(資料一覧の CSV)・refs_view(出す行・並べ替え・割り当て)・roles(割り当て)
    after(ms, 関数) → 取り消せるもの(cancel())・post(関数, *引数)(裏の糸から、この糸へ渡す)"""

    def __init__(self, view, after, post, settings: dict, save_settings, device):
        self.view, self.after, self.post = view, after, post
        self.save_settings = save_settings            # save_settings(dict): 設定に足して保存する
        self.device = device                          # device_core.DeviceWatch
        s = settings
        self.pass_code = s.get("pass", "")
        self.set_each = s.get("set_each", True)
        self.worker: threading.Thread | None = None
        self.status_busy = False
        self.started_at = 0.0
        self.step = ""
        self.cur = (0, 1)
        self.last_result = ""
        self.input_issues: list[str] = []
        self.dev = ("checking", "")
        self._logged_dev = None
        self._last_screen = ""
        self._boot_flow = False
        self._safety_at = 0.0
        self.run_turn: int | None = None
        self.run_shot = False
        self.closing: int | None = None
        self.after_state: dict[str, str] = {}
        self.run_items: list[str] = []
        self.run_state: dict[str, str] = {}
        self._input_sig = None
        self.waiting = None
        self.btn_mode, self._btn_lock, self.btn_enabled = "", None, False
        self.status_text = ""
        self.pb_value = 0.0
        self._confirm = None                          # 照射前の確認を待っている間: (plan, items, remeasure)
        self._started = False                         # start が終わったか(それまでは設定を保存しない)
        self._queued = None                           # 読んでいる最中に押されたボタンの読み直し(読み終わってから行う)
        self.lid = lid_sensor.start()                 # 蓋の磁気センサー(閉じていなければ照射しない。LID_SENSOR.md)
        am.LID_OK = self.lid.closed                   # 照射の直前に確かめる
        self.lid.add_listener(self._lid_bg)           # 切り替わったときだけ(センサーの糸から)
        self.ledger = LedgerModel(am.TRACER_DIR, am.LEDGER_STORE, self._alert, on_rows=self._check_inputs,
                                  on_lock=self._lock_info)
        self.ledger.on_values = self._values_changed
        self.ledger.on_view = lambda: self.view.ledger(self.ledger.payload())
        self.ledger.log = self._log
        self.refs = RefsModel(lambda d: self.view.refs_data(d), lambda v: self.view.refs_view(v), self._refs_use, self._alert,
                              on_roles=lambda: (self.view.roles(dict(self.refs.data.roles)), self._save()))
        self.modes = Modes([a for a in s.get("apps", UI.DEFAULT_APPS) if a not in UI.DISABLED_APPS],
                           {**UI.PHASE_COUNTS, **s.get("phase_counts", {})},
                           on_change=self._save, on_check=self._check_inputs, show=lambda: self.view.modes(self.modes.payload()))
        am.say = lambda m: self.post(self._say, m)
        am.phase_count_seen = lambda app, n: self.post(self._phases_seen, app, n)
        am.progress = lambda e, t: self.post(self._progress, e, t)

    def start(self, settings: dict):
        """組み立てた後: 設定の値を入れ、入力を調べ、機器の状態は「未接続」から始める(つなぐのは本体画面の「接続」だけ)"""
        self.ledger.set_values(settings.get("ledger_values"))
        self.refs.restore(settings.get("refs"))
        self.modes.load(settings.get("plan", UI.DEFAULT_PLAN))
        self.view.ledger(self.ledger.payload())
        self._apply_locks()
        self._check_inputs()
        self._show_aside()
        self._show_device("offline")
        self._started = True

    # ---- 画面からの操作
    def set_pass(self, code: str):
        if self._running():
            return
        self.pass_code = "".join(c for c in str(code) if c.isascii() and c.isdigit())
        if self.dev[0] in ("login", "login_nopass"):
            self._show_device("login" if self.pass_code else "login_nopass")
        self._save()

    def set_skip_phases(self, skip: bool):
        if not self._running():
            self.set_each = not skip
            self._save()

    def _values_changed(self):
        self.view.lock_info(self.ledger.locked, dict(self.ledger.values))
        self._show_aside()
        self._check_inputs()

    def _lock_info(self, on: bool, values: dict):
        """セット計測中(計測中・セットが終わるまで): 計測ファイルの欄と資料一覧からの転記を止める"""
        self.view.lock_info(on, dict(self.ledger.values))
        if hasattr(self, "refs"):
            self.refs.set_enabled(not on)

    def _refs_use(self, values: dict):
        if not self.ledger.locked:
            self.ledger.set_values({k: v for k, v in values.items() if k in self.ledger.values})

    def _phases_seen(self, app, n):
        if self.modes.learn_phase_count(app, n):
            self._save()

    # ---- ボタン
    def _set_button(self, mode: str, busy: str = "", enabled: bool = True):
        text, color = UI.MAIN_BUTTON[mode]
        prev, self.btn_mode = self.btn_mode, mode
        start = mode in START_MODES
        blocked = start and bool(self.input_issues)
        fresh = start and prev not in START_MODES
        if self._btn_lock and (fresh or not start):
            self._btn_lock.cancel(); self._btn_lock = None
        if fresh:
            self._btn_lock = self.after(UI.MAIN_BUTTON_LOCK_MS, self._unlock_button)
        self.btn_enabled = enabled and not (mode == "busy" or blocked or self._btn_lock)
        self.view.button(text.format(text=busy), color, self.btn_enabled)
        if mode in CONTINUE_MODES:
            self.view.end_button(True, enabled and not self._btn_lock)
        else:
            self.view.end_button(False, False)

    def _unlock_button(self):
        self._btn_lock = None
        if self.btn_mode in START_MODES and not self.input_issues:
            self.btn_enabled = True
            self.view.button(None, None, True)
        if self.btn_mode in CONTINUE_MODES:
            self.view.end_button(True, True)

    def main_clicked(self):
        if not self.btn_enabled:
            return
        if self.btn_mode == "login" and self._running() and self.waiting == "login":
            self._set_button("busy", UI.BUSY_TEXT["login"])
            am.request_login(self.pass_code.strip())
            return
        if self.btn_mode in CONTINUE_MODES:
            self.start_measure(remeasure=True)
            return
        {"login": self._login, "update": lambda: self._refresh_status(to_main=True), "start": self.start_measure,
         "stop": self._stop}.get(self.btn_mode, lambda: None)()

    # ---- 機器
    def _login(self):
        if self.device.alive():
            self._refresh_status(login=True)

    def device_connected(self):
        self._log("機器につながりました")
        if not self._running():
            self._refresh_status()

    def device_booted(self):
        if self._running():
            return
        if self.status_busy:
            self.after(UI.BOOTED_RETRY_MS, self.device_booted)
            return
        self._refresh_status(quiet=True, confirm=True)

    def trigger_changed(self):
        if not self._running() and not self.status_busy and self.dev[0] in ("ready", "trigger"):
            self._refresh_status(quiet=True)

    def device_lost(self):
        if not self._running() and not self.status_busy:
            self._show_device("offline")

    def device_busy(self, busy: bool):
        if self._running():
            return
        if busy:
            self._set_button("busy", UI.BUSY_TEXT["connecting"])
        elif self.btn_mode == "busy":
            self._set_button("busy", UI.MAIN_BUTTON["start"][0])

    def _running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _refresh_status(self, then=None, login=False, quiet=False, confirm=False, to_main=False):
        if self.status_busy:
            # 読んでいる最中: ボタンから(「画面更新」「ログイン」「自動計測」)なら、読み終わってから続けて行う(押しても何も
            # 起きない、にしない)。自動の読み直し(quiet)は捨てる
            if not quiet:
                self._queued = dict(then=then, login=login, confirm=confirm, to_main=to_main)
            return
        self.status_busy = True
        if to_main and self.closing is not None:
            self._end_closing()
        if not self._running() and not quiet:
            self._show_device("checking")
        passcode = self.pass_code.strip() if login else ""
        after_safety = self._last_screen == "safety"

        def job():
            try:
                op = am.Operator()
                ui = op.read()
                first = op.state(ui)
                if after_safety and first not in ("safety", "confirm", "main", "result"):
                    keep_screen(op)
                if login and op.state(ui) == "startup":
                    am.open_login(op)
                    ui = op.read()
                if login and op.state(ui) == "login":
                    if passcode:
                        try:
                            am.login(op, passcode)
                        except am.AbortError as e:
                            self.post(self._alert, *rules.alert_parts("login", desc=rules.abort_parts(e)[1]))
                            self.post(self._log, f"詳細: {e}")
                        ui = op.read()
                if (login or confirm) and op.state(ui) == "confirm":
                    am.to_main(op)
                    ui = op.read()
                if to_main and op.state(ui) in am.BACK_STATES:
                    am.back_to_main(op)
                    ui = op.read()
                st = op.state(ui)
                if st == "unknown" and getattr(dl.LINK, "booting", False):
                    st = "booting"
                info = {"ok": True, "state": st, "app": op.current_app(ui),
                        "trigger": op.trigger_state() if st in READY_STATES else "unknown", "first": first}
            except Exception as e:
                info = {"ok": False, "error": str(e), "offline": isinstance(e, OSError)}
            self.post(self._status_read, info, then)

        threading.Thread(target=job, daemon=True).start()

    def _status_read(self, info, then):
        self.status_busy = False
        if not self._running():
            self._show_status(info)
        if then:
            then(info)
        queued, self._queued = self._queued, None
        if queued and not self._running():
            self._refresh_status(**queued)

    def _show_status(self, info: dict):
        if not info["ok"]:
            self._log(f"詳細: {info['error']}")
        else:
            seen = info.get("first") or info["state"]
            if (seen, info["state"]) != (self._last_screen, self._last_screen):
                append_log(f"[画面] {seen}" + (f" → {info['state']}" if info["state"] != seen else ""))
            self._last_screen = info["state"]
        st = info.get("state") if info["ok"] else None
        if st == "safety":
            self._safety_at = time.time()
        settling = self._boot_flow and time.time() - self._safety_at < UI.SAFETY_SETTLE_S
        if st in ("booting", "safety"):
            self._boot_flow = True
        elif not (st in ("unknown", "confirm") or (settling and st in ("startup", "main"))):
            self._boot_flow = False
        if not (self._boot_flow and (st in ("unknown", "confirm") or (settling and st == "startup"))):
            self._show_device(*rules.device_status(info, bool(self.pass_code.strip())))
        if self._boot_flow and not self._running():
            self.after(UI.BOOT_POLL_MS, lambda: self._running() or self._refresh_status(quiet=True, confirm=True))

    def _set_status(self, title: str, colors=None):
        self.status_text = title
        self.view.status(title, colors)

    def _show_device(self, key: str, x: str = ""):
        self.dev = (key, x)
        if self._running():
            return
        st = self._pairing()
        left = self._pending(st)
        mode, enabled, startable = rules.main_button(key, bool(left), rules.resuming(st), self.closing is not None)
        if mode == "busy":
            self._set_button("busy", UI.BUSY_TEXT["checking"])
        elif mode == "idle":
            self._set_button("busy", UI.MAIN_BUTTON["start"][0])
        else:
            self._set_button(mode, enabled=enabled)
        if startable and self.input_issues:
            self._set_status(rules.issues_title(self.input_issues))
            self.view.detail("\n".join(self.input_issues))
            return
        color, title, desc = rules.status_parts(key, x)
        if key == "result" and left:
            desc = UI.RESULT_PENDING.format(verb=self._verb(st))
        self._set_status(title, UI.TONES[color])
        note = UI.PENDING_NOTE.format(n=len(left), apps=self._apps_text(left)) if startable and left else ""
        warn = UI.TURN_WARN_NOTE if startable and self.ledger.turn_warned(self.ledger.current_values()) else ""
        self.view.detail("\n".join(t for t in (desc, note, warn, self.last_result) if t))
        if (key, x) != self._logged_dev and key != "checking":
            self._logged_dev = (key, x)
            append_log(f"[状態] {title}" + (f" / {desc}" if desc else ""))

    # ---- 入力の不備・台帳と組みにする
    def _check_inputs(self):
        values = self.ledger.current_values()
        empty = rules.missing(values)
        st = self._pairing()
        opened = not empty and self._open_turn() is not None
        issues = rules.input_issues(self.modes.issues(), empty, st if opened else None,
                                    not empty and not opened and self.ledger.measured(values, self.modes.modes_of()),
                                    self.lid.state)
        if self.run_turn is not None:
            self.input_issues = issues
            return
        self.modes.show_states({i: v or "wait" for i, v in st.items()} if any(st.values()) else {})
        sig = (issues, tuple(self._pending(st)), self._resuming(st), self.ledger.turn_warned(values))
        if sig == self._input_sig:
            return
        self.input_issues, self._input_sig = issues, sig
        if not self._running() and not self.status_busy:
            self._show_device(*self.dev)

    def _recheck(self):
        self._input_sig = None
        self._check_inputs()

    def _apps_text(self, items) -> str:
        return "・".join(self.modes.modes_of(items))

    def _open_turn(self) -> int | None:
        values = self.ledger.current_values()
        if rules.missing(values):
            return None
        tid = self.ledger.open_turn(values)
        return None if tid == self.closing else tid

    def _apply_locks(self):
        t = self.ledger._turn(self.closing) if self.closing is not None else self.ledger.live_turn()
        if t is not None and keys_of(self.ledger.current_values()) != t["keys"]:
            self.ledger.set_values(dict(zip(UI.DUPLICATE_KEYS, t["keys"])))
        self.modes.locked = t is not None
        self.ledger.lock_inputs(t is not None)

    def _end_closing(self):
        tid, self.closing, self.after_state = self.closing, None, {}
        done, total = self.ledger.turn_done(tid) if tid is not None else (0, 0)
        if tid is not None:
            self.ledger.close_turn(tid)
        self._apply_locks()
        if total and done >= total:
            self.ledger.set_values({k: "" for k in UI.CLEAR_ON_DONE})
        self._recheck()

    def _resuming(self, st=None) -> bool:
        return rules.resuming(self._pairing() if st is None else st)

    def _verb(self, st=None) -> str:
        return rules.verb(self._pairing() if st is None else st)

    def end_turn(self):
        """「終了」(再計測・再開の横)"""
        if self._running() or self.status_busy or (tid := self._open_turn()) is None:
            return
        left = self._pending()
        k = self.ledger.abort_turn(tid)
        self._apply_locks()
        self._log(UI.END_TURN_LOG.format(n=len(left), k=k))
        self._recheck()

    def _pairing(self) -> dict[str, str]:
        if self.run_turn is not None:
            return dict(self.run_state)
        items = self.modes.ids()
        if self.after_state:
            return {i: self.after_state.get(i, "") for i in items}
        tid = self._open_turn()
        if tid is None:
            return {i: "" for i in items}
        return dict(zip(items, self.ledger.pair(tid, self.modes.modes_of(items))))

    def _pending(self, st=None) -> list[str]:
        return rules.pending(self._pairing() if st is None else st)

    # ---- 実行
    def _show_aside(self, force: bool = False):
        if self._running() and not force:
            return
        v = self.ledger.current_values()
        self.view.aside({k: v.get(k, "") for k in UI.STATUS_ASIDE})

    def _set_running(self, running: bool):
        self.modes.locked = running
        self.ledger.lock_inputs(running)
        self.view.progress(running, self.pb_value)
        if running:
            self._set_button("stop", enabled=False)       # 停止は照射中だけ押せる(照射の進みが届いたら押せる)
        self.device.manual = not running
        self.view.manual(not running)
        self.view.lock_settings(running)
        if running:
            self.view.select_log()

    def start_measure(self, remeasure: bool = False):
        """「自動計測」/「再計測」「再開」(remeasure)"""
        if self._running() or self._confirm is not None:
            return
        self.ledger.finish()
        self._check_inputs()
        pending = self._pending()
        if remeasure != bool(pending):
            return
        items = pending if remeasure else self.modes.ids()
        plan = list(zip(self.modes.modes_of(items), map(self.modes.phases, items)))
        if self.input_issues or not plan:
            return
        self.view.detail("")
        self._set_status("状態確認中")
        self._set_button("busy", UI.BUSY_TEXT["checking"])
        self._refresh_status(then=lambda info: self._start_confirm(info, plan, items, remeasure), to_main=remeasure)

    def _start_confirm(self, info, plan, items, remeasure=False):
        key, x = rules.device_status(info, bool(self.pass_code.strip()))
        if key not in UI.STARTABLE:
            _, title, desc = rules.status_parts(key, x)
            self.view.message(UI.CANNOT_START, f"{title}。\n{desc}。" if desc else f"{title}。")
            return
        self._confirm = (plan, list(items), remeasure)
        self.view.confirm(UI.DANGER_TITLE, UI.DANGER_LEAD.format(n=len(plan)), list(UI.DANGER_CHECKS), UI.DANGER_OK)

    def confirm_answer(self, ok: bool, checks: list) -> bool:
        """照射前の確認の答え。照射してよいのは「照射開始」で、安全確認がすべてチェックされているときだけ(ここで必ず確かめる)。
        照射を始めたら True"""
        pending, self._confirm = self._confirm, None
        if pending is None:
            return False
        if not (ok is True and len(checks) == len(UI.DANGER_CHECKS) and all(c is True for c in checks)):
            self._show_device(*self.dev)
            return False
        plan, items, remeasure = pending
        am.PASSCODE = ""                              # 開始・測定の途中では自動でログインしない
        self._save()
        am.CANCEL.clear()
        if remeasure and self._pending() != list(items):
            self._show_device(*self.dev)
            return False
        values = self.ledger.current_values()
        before = self._pairing() if remeasure else {}
        verb = self._verb() if remeasure else UI.LBL_AUTO
        self.closing, self.after_state = None, {}
        self.run_turn = ((self._open_turn() if remeasure else None) or self.ledger.begin_turn(values, self.modes.modes_of()))
        self.run_shot = False
        self.run_items = list(items)
        self.run_state = rules.run_states(self.modes.ids(), items, before)
        self.modes.show_states(self.run_state)
        self._show_aside(force=True)
        self.pb_value = 0.0
        self._set_running(True)
        self._set_status("切替中")
        self.view.detail("")
        self.started_at = time.time()
        self.cur = (0, len(plan))
        self._log(f"{verb} ({len(plan)} 回)")
        self.worker = threading.Thread(target=self._run, args=(plan, self.set_each, self.ledger.folder), daemon=True)
        self.worker.start()
        return True

    def _fetch(self, rows: list[dict], folder: str):
        nos = [r["sample_no"] for r in rows if r.get("sample_no")]
        for r in rows:
            if not r.get("sample_no"):
                self.post(self._alert, *rules.alert_parts("pdz_no", app=r["app"]))
        if not nos:
            return
        self.post(self._stage, "取得中")
        try:
            link = dl.current_link() or dl.LINK
            saved, missing = fetch_measurements(folder, nos, **({"host": link.host, "source": link.source} if link else {}))
            self.post(self._log, f"取込 pdz {len(saved) - 1} 件・{RESULTS}")
            if missing:
                self.post(self._alert, *rules.alert_parts("pdz_missing", nos=", ".join(missing)))
        except Exception as e:
            self.post(self._alert, *rules.alert_parts("fetch", nos=", ".join(nos), err=e))

    def _results_after(self, last_no: int) -> list[dict]:
        link = dl.current_link() or dl.LINK
        rows = results_rows(**({"host": link.host, "source": link.source} if link else {}))
        return [r for r in rows if (file_no(r) or 0) > last_no]

    def _last_no(self) -> int | None:
        try:
            nos = [file_no(r) for r in self._results_after(-1)]
            return max((n for n in nos if n is not None), default=0)
        except Exception as e:
            self.post(self._log, f"詳細: 機器の結果表を読めませんでした({e})。途中で切れたときに結果画面が消えていると再開できません")
            return None

    def _run(self, plan, set_each: bool, folder: str):
        op, rows, end = None, [], "完了"
        try:
            op = am.Operator()
            am.run_plan(op, plan, set_each,
                        on_step=lambda i, n, a, s: self.post(self._on_step, i, n, a, s),
                        on_result=lambda row: (rows.append(row), self.post(self._on_result, row)),
                        on_stage=lambda stage: self.post(self._stage, stage),
                        on_wait=lambda kind: self.post(self._on_wait, kind),
                        results_after=self._results_after, last_no=self._last_no())
        except am.CutStop:
            end = "接続切れで停止"
        except am.Cancelled:
            end = "停止しました"
        except am.LidOpen as e:                       # 蓋が開いていた・途中で開いた(照射中ならトリガーを離した。その回は機器エラー)
            title, desc = rules.abort_parts(e)
            self.post(self._alert, *rules.alert_parts("abort", title=title, desc=desc))
            self.post(self._log, f"詳細: {e}")
            end = UI.LID_END
        except (am.AbortError, ConnectionError, TimeoutError, RuntimeError) as e:
            if op:
                op.save_error()
            title, desc = rules.abort_parts(e)
            self.post(self._alert, *rules.alert_parts("abort", title=title, desc=desc))
            self.post(self._log, f"詳細: {e}")
            end = "エラーで中断"
        except Exception:
            self.post(self._alert, *rules.alert_parts("unexpected"))
            self.post(self._log, traceback.format_exc())
            end = "エラーで中断"
        self._fetch(rows, folder)
        self.post(self._done, end)

    def _lid_bg(self, state: str):
        """(センサーの糸から)蓋の状態が切り替わった: 閉じていなければ測定の途中なら止め(照射中はトリガーを離す)、この糸へ渡す"""
        if state != lid_sensor.CLOSED:
            am.lid_opened()
        self.post(self._lid_changed, state, time.time())

    def _lid_changed(self, state: str, at: float):
        append_log(f"[蓋] {state}({dl.ms_stamp(at)})")
        self._check_inputs()                          # 入力の不備として状態の欄・ボタンに映す

    def _stop(self):
        if self._running():
            am.CANCEL.set()
            self._set_status("停止中")
            self._set_button("busy", UI.BUSY_TEXT["stopping"])
            self._log("停止要求(トリガー解除)")

    # ---- 測定の知らせ(裏の糸から post で)
    def _on_step(self, i, n, a, s):
        self.cur, self.step = (i, n), f"{i}/{n} {a}"
        if 0 < i <= len(self.run_items):
            self.run_state[self.run_items[i - 1]] = "run"
            self.modes.show_states(self.run_state)
        self.view.detail(f"{self.step}\n{s:g} 秒")
        self.pb_value = (i - 1) / n
        self.view.progress(True, self.pb_value)

    def _stage(self, v):
        if self.status_text != "停止中" and not self.waiting:
            self._set_status(v)
        if v == "取得中" and self.btn_mode == "stop":
            self._set_button("busy", UI.BUSY_TEXT["fetching"])
        elif self.btn_mode == "stop":
            self._set_button("stop", enabled=False)

    def _progress(self, e, t):
        if self.status_text in ("結果確認中", "照射停止"):   # 照射の途中で切れた後: 照射は止まっているので進みを出さない
            return
        if self.btn_mode == "stop" and not self.btn_enabled:
            self._set_button("stop")
        i, n = self.cur
        self.pb_value = rules.progress(i, n, e, t)
        self.view.progress(True, self.pb_value)
        self.view.detail(rules.progress_text(self.step, e, t))

    def _on_result(self, v):
        if self.btn_mode == "stop":
            self._set_button("stop", enabled=False)
        no = v.get("sample_no", "")
        self.run_shot = True
        i = self.cur[0]
        if 0 < i <= len(self.run_items):
            self.run_state[self.run_items[i - 1]] = "fail" if v.get("failed") else "done"
            self.modes.show_states(self.run_state)
        self.ledger.add_measurement(no, v["app"], failed=v.get("failed", ""), turn=self.run_turn)
        self._log(f"{'失敗' if v.get('failed') else '完了'} No.{no or '?'} {v['app']}")
        if v.get("failed"):
            self._alert(*rules.alert_parts("failed", no=no or "?", app=v["app"], reason=v["failed"]))
        elif v.get("mismatch"):
            self._alert(*rules.alert_parts("mismatch", no=no or "?", app=v["app"], msg=v["mismatch"]))

    def _done(self, v):
        if self.worker is not None:
            self.worker.join(timeout=2)
        self.waiting = None
        minutes = (time.time() - self.started_at) / 60
        self.last_result = UI.LAST_RESULT.format(end=v, min=minutes)
        self._set_status(v)
        self.view.detail(self.last_result)
        am.CANCEL.clear()
        shot = self.run_shot
        self._finish_turn()
        self._show_aside(force=True)
        self._log(f"{v}({minutes:.1f} 分)")
        self._refresh_status(to_main=not shot)

    def _finish_turn(self):
        tid, shot = self.run_turn, self.run_shot
        state = {i: "wait" if s == "run" else s for i, s in self.run_state.items()}
        self.run_turn, self.run_items, self.run_state = None, [], {}
        self._set_running(False)
        done, total = self.ledger.turn_done(tid) if tid is not None else (0, 0)
        end = rules.turn_end(shot, done, total)
        if end == "drop":
            self.ledger.drop_turn(tid)
        elif end == "close" and self.ledger._turn(tid) is not None:
            self.after_state, self.closing = state, tid
        self._apply_locks()
        self._recheck()
        if left := self._pending():
            self._log(UI.PENDING_LOG.format(n=len(left), verb=self._verb(), apps=self._apps_text(left)))

    def _on_wait(self, kind: str):
        if self.status_text == "停止中":
            return
        if kind == "resumed":
            self.waiting = None
            self._log("再開しました")
            self.view.detail(self.step)
            self._set_button("stop", enabled=False)
            return
        self.waiting = "login" if kind == "login_failed" else kind
        title, desc = UI.RESUME_WAIT[kind]
        self._set_status(title)
        self.view.detail(f"{self.step}\n{desc}")
        self._log(f"再開待ち: {title}・{desc}")
        if self.waiting == "login":
            self._set_button("login")
        else:
            self._set_button("stop", enabled=False)

    # ---- ログ・アラート・設定
    def _say(self, msg: str):
        m = msg.strip()
        if m.startswith("[ずれ]"):
            return
        if m.startswith("[") or any(w in m for w in ("完了", "解除", "注意", "停止", "ログイン", "測定時間", "トリガー操作",
                                                     "測定の続き", "測定は終わって", "つなぎ直", "照射されていません", "機器の記録",
                                                     "測り直", "題名を覚え")):
            if m.startswith("完了 No."):
                return
            self._log(m)

    def _log(self, msg: str):
        self.view.log(msg)
        append_log(msg)

    def _alert(self, title: str, desc: str = ""):
        append_log(f"[アラート] {title}" + (f" / {desc}" if desc else ""))
        self.view.alert(title, desc)

    def _save(self):
        if not self._started:                          # 組み立てている途中(測定モード表を読む前など)は保存しない
            return
        try:
            self.save_settings({"plan": self.modes.plan(), "set_each": self.set_each, "apps": self.modes.apps,
                                "phase_counts": self.modes.phase_counts, "ledger_values": self.ledger.current_values(),
                                "refs": self.refs.settings(), "pass": self.pass_code.strip()})
        except Exception:
            append_log("[設定を保存できない] " + traceback.format_exc(limit=2))

    # ---- 閉じる
    def running(self) -> bool:
        return self._running()

    def shutdown(self, wait_s: float = 10.0) -> bool:
        """閉じる前の後始末: 測定中なら止めて(照射中ならトリガーを離す)待ち、念のためトリガーが離れていることを確かめ、保存して
        機器との接続を切る。照射が止まった・照射していなければ True(確認は画面の側で先に済ませる)"""
        if self._running():
            am.CANCEL.set()
            t = time.time()
            while self.worker.is_alive() and time.time() - t < wait_s:
                self.worker.join(timeout=0.1)
        released = release_trigger(log=self._log)
        self.ledger.finish()
        self._save()
        self.device.close()
        return released
