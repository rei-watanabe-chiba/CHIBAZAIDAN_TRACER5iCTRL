"""画面を持たない処理(ledger_core・refs_core・modes_core・app_rules)の確認。Tk を使わない(WebView2 版でもそのまま使う)

    python tests/core_test.py

本物の config/ と保存先は使わない(一時フォルダ)。結果は1行ずつ OK / NG。NG が1つでもあれば終了コード 1
"""
from __future__ import annotations

import json, os, shutil, sys, tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Traer5iAuto", "src")
sys.path.insert(0, SRC)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

results: list[bool] = []


def check(name: str, cond, detail=""):
    results.append(bool(cond))
    print(f"{'OK' if cond else 'NG'}  {name}" + ("" if cond else f"  -> {detail}"))


import app_rules as rules                                   # noqa: E402
import ledger_core as lc                                    # noqa: E402
import modes_core as mc                                     # noqa: E402
import refs_core as rc                                      # noqa: E402
from gui_config import UI                                   # noqa: E402

check("切り出した処理は Tk を読み込まない(画面を作り替えてもそのまま使える)",
      not any(m == "tkinter" or m.startswith("tkinter.") or m == "_tkinter" for m in sys.modules),
      [m for m in sys.modules if "tk" in m.lower()])

tmp = tempfile.mkdtemp(prefix="core_test_")
try:
    # ---------------------------------------------------------------- 台帳(ledger_core)
    errors = []
    book = lc.LedgerBook(os.path.join(tmp, "TracerData"), os.path.join(tmp, "config", "ledger.json"),
                         on_error=lambda kind, err: errors.append(kind))
    os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
    book.load()
    check("台帳: ファイルが無ければ空で始まる", not book.rows and not book.turns and not errors)
    v = {"Database": "DB", "ID": "A-1", "subID": "1", "項目1": "x", "項目2": ""}
    tid = book.begin_turn(v, ["Obsidian", "Mudrock Air", "Obsidian"])
    r1 = book.add_measurement("901", "Obsidian", v, turn=tid)
    r2 = book.add_measurement("902", "Mudrock Air", v, failed="停止エラー", turn=tid)
    check("台帳: 組み合わせ(有効 = 済み / 失敗だけ = ✕ / 無し = まだ)",
          book.pair(tid, ["Obsidian", "Mudrock Air", "Obsidian"]) == ["done", "fail", ""],
          book.pair(tid, ["Obsidian", "Mudrock Air", "Obsidian"]))
    check("台帳: セットの済みの数", book.turn_done(tid) == (1, 3), book.turn_done(tid))
    check("台帳: 開いているセットは今の計測ファイルのもの", book.open_turn(v) == tid and book.open_turn({**v, "subID": "2"}) is None)
    check("台帳: 開いているセットの行は編集できない(locked)", book.locked([r1]) and not book.set_failed([r1], "x"))
    check("台帳: 表示(状態の文・セットの文・色)", book.shown(r2) == (f"{UI.LEDGER_LIVE}・✕ 停止エラー", "S1-2", "failed")
          and book.shown(r1)[2] == "live", (book.shown(r1), book.shown(r2)))
    check("台帳: 保存(ledger.json と File.csv。File.csv は有効な行だけ)", book.save() and not book.dirty)
    data = json.load(open(book.store, encoding="utf-8"))
    fcsv = open(book.path(), encoding="utf-8-sig").read()
    check("台帳: ledger.json に行とセット、File.csv に失敗の行は出ない",
          len(data["rows"]) == 2 and data["turns"][0]["recorded"] == [["901", 1], ["902", 2]]
          and "901" in fcsv and "902" not in fcsv and UI.LEDGER_STATE_COL not in fcsv.splitlines()[0], fcsv)
    n = book.abort_turn(tid)
    check("台帳: 「終了」で済みの回も「セット中断」で無効にして閉じる",
          n == 1 and book.rows[r1]["failed"] == UI.LEDGER_FAILED_ABORT and book.turn(tid)["closed"] and not book.locked([r1]))
    check("台帳: 閉じたセットの行は有効 / 失敗を切り替えられる", book.set_failed([r1], "") and not book.failed(r1))
    check("台帳: 計測済み(同じ計測ファイル・モードの有効な行がある)",
          book.measured(v, ["Obsidian"]) and not book.measured(v, ["Glass Dual"]) and not book.measured({**v, "subID": "2"}, ["Obsidian"]))
    check("台帳: セルの値を変える(変わったときだけ True)", book.set_cell(r1, "項目2", "y") and not book.set_cell(r1, "項目2", "y"))
    book.set_cell(r1, "ID", "B-9"); book.mark()
    check("台帳: セットと違う計測ファイルに書き換えた行は「不一致」", book.warn.get(r1) == ["mismatch"]
          and book.turn_warned({**v, "ID": "B-9"}) and book.shown(r1)[2] == "warn", book.warn)
    book.delete([r2]); book.mark()
    check("台帳: セットで入れた回が消えたら残りの行は「欠け」", "missing" in book.warn.get(r1, []), book.warn)
    t2 = book.begin_turn({**v, "subID": "3"}, ["Obsidian"])
    check("台帳: 照射の無かったセットは記録を残さない(drop)", book.drop_turn(t2) and book.turn(t2) is None)
    t3 = book.begin_turn({**v, "subID": "4"}, ["Obsidian", "Obsidian"])
    book.add_measurement("903", "Obsidian", {**v, "subID": "4"}, turn=t3)
    book.save()
    again = lc.LedgerBook(book.folder, book.store)
    again.load()
    check("台帳: 読み直すと同じ(一部済みのセットは開いたまま)",
          [r["No"] for r in again.rows.values()] == ["901", "903"] and again.live_turn()["id"] == t3)
    book.turn(t3)["closed"] = False; book.turns.append({"id": 99, "start": "", "keys": [], "plan": [], "recorded": []})
    check("台帳: 整える(行の無いセットは捨てる)", book.settle() and book.turn(99) is None)

    # 前の版の ledger.csv からの移し替え
    mig = os.path.join(tmp, "mig")
    os.makedirs(mig)
    with open(os.path.join(mig, "ledger.csv"), "w", encoding="utf-8-sig") as f:
        f.write("No,モード,状態,Database,ID,subID,項目1,項目2,備考,日時\n7001,Obsidian,,D,M,1,,,,t\n7002,Obsidian,✕ 停止エラー,D,M,1,,,,t\n")
    b2 = lc.LedgerBook(os.path.join(tmp, "TD2"), os.path.join(mig, "ledger.json"))
    b2.load()
    check("台帳: ledger.json が無ければ前の版の ledger.csv から作る(「✕ 理由」は失敗)",
          [(r["No"], r["failed"]) for r in b2.rows.values()] == [("7001", ""), ("7002", "停止エラー")]
          and os.path.exists(b2.store))

    # ---------------------------------------------------------------- 資料一覧(refs_core)
    p = os.path.join(tmp, "refs.csv")
    with open(p, "w", encoding="cp932", newline="") as f:
        f.write("資料ID,名称,地区\n\nA-002,黒曜石 b,北\nA-010,黒曜石 a,南\n\"A-001\",\"改行\nあり\",北\n")
    rb = rc.RefBook()
    rb.load(p)
    check("資料一覧: Shift_JIS を読み、ID は「ID」を含む列・行番号は元のファイルの行",
          rb.head == ["資料ID", "名称", "地区"] and rb.roles == {"ID": "資料ID"} and rb.lines == [3, 4, 5], (rb.roles, rb.lines))
    check("資料一覧: あいまい検索(全角・ハイフンを無視・一致の良い順)",
          rb.matches("ａ００１", "資料ID") == [2] and rb.matches("a0", "資料ID") == [0, 1, 2])
    check("資料一覧: 全列の検索は 文字が順番どおり を使わない", rb.matches("北") == [0, 2] and rb.matches("a2x") == [])
    rb.sort_by("資料ID")
    check("資料一覧: 並べ替え 昇順 → 降順 → 元の順", rb.view() == [2, 0, 1] and (rb.sort_by("資料ID") or rb.view()) == [1, 0, 2]
          and (rb.sort_by("資料ID") or rb.sort) is None)
    rb.sort_by(rc.NUM); rb.sort_by(rc.NUM)
    check("資料一覧: # の列は元の行番号の順", rb.view() == [2, 1, 0])
    rb.sort = None
    rb.assign("名称", "項目1")
    old, new = rb.set_join(("地区", "-", "資料ID"))
    check("資料一覧: 結合列は一番左・値は 列A + 区切り + 列B",
          new == "地区+資料ID" and rb.cols[0] == new and rb.rows[0][0] == "北-A-002", (new, rb.cols, rb.rows[0]))
    rb.assign(new, "ID")
    check("資料一覧: 割り当てた役割ごとの値(計測ファイルの欄へ)", rb.values(1) == {"ID": "南-A-010", "項目1": "黒曜石 a"}, rb.values(1))
    check("資料一覧: 同じ内容なら「更新」は押せない", not rb.can_join("地区", "-", "資料ID") and rb.can_join("地区", "", "資料ID"))
    rb.set_join(("地区", "", "資料ID"))
    check("資料一覧: 作り直すと割り当ては新しい列へ移す", rb.roles["ID"] == "地区+資料ID" and rb.rows[0][0] == "北A-002")
    rb.set_join(None)
    check("資料一覧: 消すと割り当ても外す", "ID" not in rb.roles and rb.cols == rb.head)
    rb.assign("資料ID", "ID"); rb.assign("資料ID", "項目2")
    check("資料一覧: 1つの列は1つの役割だけ", rb.roles == {"項目1": "名称", "項目2": "資料ID"}, rb.roles)

    # ---------------------------------------------------------------- 測定モード(modes_core)
    check("モード: 秒数は整数だけ・範囲内", mc.parse_phases({"p1": " 30 ", "p2": "50.0"}) == {"p1": "30", "p2": "50"}
          and mc.phases_bad({"p1": "1.5"}) and mc.phases_bad({"p1": ""}) and mc.phases_bad({"p1": "0"}) and not mc.phases_bad({"p1": "3600"}))
    check("モード: 初期値(Obsidian 120 / Mudrock Air 30+50 / ほかはフェーズ数で)",
          mc.default_times("Obsidian", {}) == ["120", "", ""] and mc.default_times("Mudrock Air", {"Mudrock Air": 2}) == ["30", "50", ""]
          and mc.default_times("Glass Dual", {"Glass Dual": 2}) == ["30", "50", ""]
          and mc.default_times("X", {"X": 3}) == [str(UI.DEFAULT_SEC)] * 3)
    check("モード: 保存した計画を読む(旧形式・使わないモードは外す)",
          mc.saved_rows([["Obsidian", [120.0]], ["Mudrock Air", 30, 2], [UI.DISABLED_APPS[0], [60]]])
          == [("Obsidian", ["120"]), ("Mudrock Air", ["30"])])
    check("モード: 入力の不備(行が無い / 不正な値)", mc.issues([]) == [UI.INPUT_ISSUES["no_modes"]]
          and mc.issues([False, True]) == [UI.INPUT_ISSUES["phase"]] and mc.issues([False]) == [])
    check("モード: 合計の文", mc.total_text([("A", [30.0, 50.0]), ("B", [120.0])]) == "測定 2 回 / 200 秒")

    # ---------------------------------------------------------------- 画面の決まり(app_rules)
    ok = {"ok": True, "app": "Obsidian"}
    check("決まり: 機器の状態", rules.device_status({**ok, "state": "main"}, True) == ("ready", "Obsidian")
          and rules.device_status({**ok, "state": "main", "trigger": "release"}, True)[0] == "trigger"
          and rules.device_status({**ok, "state": "login"}, False)[0] == "login_nopass"
          and rules.device_status({"ok": False, "offline": True}, True)[0] == "offline")
    check("決まり: 入力の不備(空の必須項目 / 開いているセットが全部済み / 計測済み)",
          rules.input_issues([], ["ID"], None, True) == [UI.INPUT_ISSUES["required"].format(fields="ID")]
          and rules.input_issues([], [], {"a": "done"}, False) == [UI.INPUT_ISSUES["done"]]
          and rules.input_issues([], [], {"a": "done", "b": ""}, True) == []
          and rules.input_issues([], [], None, True) == [UI.INPUT_ISSUES["done"]])
    st = {"a": "done", "b": "fail", "c": ""}
    check("決まり: 未了と「再計測」/「再開」", rules.pending(st) == ["b", "c"] and not rules.resuming(st)
          and rules.resuming({"a": "done", "c": ""}) and rules.pending({"a": "done"}) == [] and rules.pending({"a": ""}) == [])
    check("決まり: メインのボタン",
          rules.main_button("ready", False, False, False) == ("start", True, True)
          and rules.main_button("ready", True, False, False)[0] == "remeasure"
          and rules.main_button("result", True, True, False)[0] == "resume"
          and rules.main_button("ready", False, False, True)[:2] == ("update", True)
          and rules.main_button("login_nopass", False, False, False)[:2] == ("login", False)
          and rules.main_button("offline", False, False, False)[:2] == ("idle", False))
    check("決まり: セットの終わり方", rules.turn_end(False, 0, 3) == "drop" and rules.turn_end(True, 0, 3) == "close"
          and rules.turn_end(True, 3, 3) == "close" and rules.turn_end(True, 1, 3) == "open")
    check("決まり: 照射中の進みの文", rules.progress_text("2/4 Mudrock Air", 18, 30) == "2/4 Mudrock Air\n30 秒 ・ 残り 12 秒"
          and rules.progress_text("s", 33, 30).endswith("終了待ち +3 秒") and rules.progress(2, 4, 15, 30) == 0.375)
    check("決まり: 計測を始めるときの行の状態", rules.run_states(["a", "b"], ["b"], {"a": "done", "b": "fail"})
          == {"a": "done", "b": "wait"})
    # ---------------------------------------------------------------- 蓋の磁気センサー(lid_sensor。LID_SENSOR.md)
    check("決まり: 蓋が閉じていなければ入力の不備の先頭に「蓋：…」(閉じていれば何も足さない)",
          rules.input_issues(["x"], [], None, False, "open") == [UI.INPUT_ISSUES["lid_open"], "x"]
          and rules.input_issues([], [], None, False, "none") == [UI.INPUT_ISSUES["lid_none"]]
          and rules.input_issues([], [], None, False, "closed") == [] and rules.input_issues([], [], None, False) == [])
    check("蓋: 開いていればタイトルは「蓋なし」、センサーなしは「センサー未接続」・ほかは「入力エラー」",
          rules.issues_title(rules.input_issues(["x"], [], None, False, "open")) == UI.LBL_LID_OPEN == "蓋なし"
          and rules.issues_title(rules.input_issues([], [], None, False, "none")) == UI.LBL_LID_NONE == "センサー未接続"
          and rules.issues_title(["x"]) == UI.LBL_INPUT_ERROR and rules.status_look(UI.LBL_INPUT_ERROR)["icon"] == "✕")
    # ---------------------------------------------------------------- 状態の欄のカテゴリー(2026-11-26)
    look = lambda t, c=None: (rules.status_look(t, c)["bg"], rules.status_look(t, c)["icon"])
    tone = lambda k: UI.TONES[UI.DEVICE_STATUS[k][0]]
    check("状態の欄: 安全の注意(蓋なし・センサー未接続・手動動作中)は黄の地に ⚠",
          look(UI.LBL_LID_OPEN) == look(UI.LBL_LID_NONE) == look("手動動作中", tone("trigger")) == (UI._YELLOW[0], "⚠"))
    check("状態の欄: 操作待ちは緑の地に ☞(ログイン待ち・安全確認待ち・結果画面・操作中)",
          all(look(UI.DEVICE_STATUS[k][1], tone(k)) == (UI._GREEN[0], "☞")
              for k in ("login", "login_nopass", "safety", "result", "busy", "busy_manual")))
    check("状態の欄: 進行中は緑の地に砂時計の線の絵(停止中も)",
          all(look(t) == (UI._GREEN[0], "ico-hourglass") for t in ("切替中", "設定中", "取得中", "結果確認中", "停止中", "起動中")))
    check("状態の欄: 止めた終わり方は赤の地に ■、不具合は ✕",
          all(look(t) == (UI._RED[0], "■") for t in ("停止しました", UI.LID_END, "接続切れで停止"))
          and all(look(t) == (UI._RED[0], "✕") for t in ("エラーで中断", "入力エラー", "接続切れ", "照射停止"))
          and all(look(UI.DEVICE_STATUS[k][1], tone(k))[1] == "✕" for k in ("unknown", "offline", "error")))
    check("状態の欄: 準備OK・完了は青の ✓、確認中は灰の ◌(機器の状態と測定の初めで同じ名前)・照射中は ☢",
          look("完了") == look("準備OK", tone("ready")) == (UI._BLUE[0], "✓")
          and UI.DEVICE_STATUS["checking"][1] == "状態確認中" and look("状態確認中") == (UI._GRAY[0], "◌")
          and rules.status_look("照射中")["icon"] == "☢" and "待機中" not in UI.STATUS_COLORS)
    import lid_sensor as ls
    seen = []
    f = ls.Fixed(ls.OPEN)
    f.add_listener(seen.append)
    f.set(ls.OPEN); f.set(ls.CLOSED); f.set(ls.CLOSED); f.set(ls.NONE)
    check("蓋: 知らせるのは切り替わったときだけ", seen == [ls.CLOSED, ls.NONE] and not f.closed(), seen)
    s_ = ls.LidSensor.__new__(ls.LidSensor)        # 糸を動かさずに判定だけを確かめる
    ls._Listeners.__init__(s_)
    s_.gap_s, s_._held, s_._last, s_._checked, s_._names = 0.8, False, 0.0, 0.0, {}
    present = {"v": True}
    s_._find = lambda u32: present["v"]
    seen = []
    s_.add_listener(seen.append)
    s_._eval(None, 10.0)
    s_._held, s_._last = True, 10.0
    s_._eval(None, 10.5)
    st1 = s_.state
    s_._eval(None, 11.0)
    st2 = s_.state
    s_._held, s_._last = True, 12.0
    s_._eval(None, 12.1)
    present["v"] = False
    s_._eval(None, 13.2)
    check("蓋: 押したままなら閉・連打が途絶えたら開・USB が無ければなし(照射しない側に倒す)",
          st1 == ls.CLOSED and st2 == ls.OPEN and s_.state == ls.NONE
          and seen == [ls.OPEN, ls.CLOSED, ls.OPEN, ls.CLOSED, ls.NONE], (st1, st2, s_.state, seen))
    check("蓋: 連打の途絶えの判定は Windows のキーの設定から(0.25〜1 秒)", 0.25 <= ls.repeat_gap_s() <= 1.0)
    check("蓋: センサーを読む処理も Tk を読み込まない", "tkinter" not in sys.modules)
    import app_rules as ar
    check("小窓: モード・画面はそれぞれ入り切り・「なし」は全部切る",
          ar.toggle_mini([], "screen") == ["screen"] and ar.toggle_mini(["screen"], "modes") == ["modes", "screen"]
          and ar.toggle_mini(["modes", "screen"], "modes") == ["screen"] and ar.toggle_mini(["modes", "screen"], "none") == [])
    check("小窓: 前の版の設定を読み替える・知らない値は何も出さない",
          ar.mini_parts("status") == [] and ar.mini_parts("modes") == ["modes"] and ar.mini_parts("all") == ["modes", "screen"]
          and ar.mini_parts("x") == [] and ar.mini_parts(None) == [] and ar.mini_parts(["screen", "x"]) == ["screen"])
    import storage
    name = rf"Local\Traer5iAuto_test_{os.getpid()}"
    check("二重起動: 1 つ目は起動でき、同じ印の 2 つ目は起動しない", storage.single_instance(name) and not storage.single_instance(name))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if all(results) else 1)
