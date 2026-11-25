"""測定台帳の中身(画面を持たない)。正本 config/ledger.json(台帳の行とセットの記録を 1 つのファイルに)の読み書き、
保存先の File.csv の書き出し、セット(コードの中の名前は turn)の記録、測定モード表との組み合わせ、警告の判定。
画面の側(controller.LedgerModel)はこれを呼んで表に映すだけにする(2026-11-19: WebView2 へ作り替える前に、処理と画面を分けた)"""
from __future__ import annotations

import csv, io, json, os, time
from collections import Counter

from gui_config import UI
from storage import write_atomic


def keys_of(values: dict) -> list[str]:
    """セットを見分ける計測ファイルの値(Database・ID・subID)"""
    return [str(values.get(k, "") or "").strip() for k in UI.DUPLICATE_KEYS]


def cols() -> list[str]:
    """行の中身として持つ列(表示だけの「状態」「セット」は除く)"""
    return [c[0] for c in UI.LEDGER_COLS if c[0] not in (UI.LEDGER_STATE_COL, UI.LEDGER_TURN_COL)]


class LedgerBook:
    """台帳の行(rows: 行の名前 -> 中身。並びは入れた順)とセットの記録(turns)。
    行の中身 = 列の値・failed(失敗の理由。空なら有効)・turn(セットの番号)・seq(そのセットで何回目に入ったか)。
    セットの記録 = {id, start, keys(Database・ID・subID), plan(測定モードの並び), recorded: [[No, seq], ...], closed}。
    on_error(種類, 例外): 読めない・保存できないとき(画面がアラート欄に出す)"""

    def __init__(self, folder: str, store: str = "", on_error=lambda kind, err: None):
        self.folder = folder
        self.store = store or os.path.join(folder, "ledger.json")
        self.on_error = on_error
        self.rows: dict[str, dict] = {}
        self.turns: list[dict] = []
        self.warn: dict[str, list[str]] = {}          # 行の名前 -> 警告(UI.LEDGER_WARN のキー)
        self.dirty = False
        self._seq = 0
        os.makedirs(folder, exist_ok=True)

    # ---- ファイル
    def path(self) -> str:
        return os.path.join(self.folder, UI.LEDGER_FILE)     # 管理簿は保存先の File.csv 1つ

    def load(self):
        """正本(ledger.json)を読む。まだ無ければ前の版の表示用の台帳(同じ場所の ledger.csv。「状態」=「✕ 理由」は失敗)か、
        保存先の File.csv(全部有効)を取り込んで ledger.json を作る(前の版のファイルは消さない)"""
        self.rows, self.turns = {}, []
        data = None
        if os.path.exists(self.store):
            try:
                with open(self.store, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                self.on_error("ledger_read", e)
        if data is not None:
            self.turns = list(data.get("turns", []))
            for row in data.get("rows", []):
                self.insert(row)
        else:
            old = os.path.splitext(self.store)[0] + ".csv"
            p = old if os.path.exists(old) else self.path()
            if os.path.exists(p):
                with open(p, encoding="utf-8-sig", newline="") as f:
                    for r in csv.DictReader(f):
                        state = (r.get(UI.LEDGER_STATE_COL) or "").strip()
                        failed = state[len(UI.LEDGER_FAILED.format(reason="")):] if state else ""
                        self.insert({**{c: r.get(c, "") or "" for c in cols()}, "failed": failed})
                self.save()
        if self.settle():
            self.save()
        self.mark()

    def insert(self, row: dict) -> str:
        """行を最後に足して、その名前を返す(保存はしない)"""
        self._seq += 1
        rid = f"r{self._seq}"
        self.rows[rid] = {**{c: str(row.get(c, "") or "") for c in cols()}, "failed": row.get("failed", "") or "",
                          "turn": row.get("turn"), "seq": row.get("seq")}
        return rid

    def save(self) -> bool:
        """全体を書き直す(一時ファイル経由): 正本 ledger.json(全部の行とセットの記録)と、保存先の File.csv(有効な行だけ・
        「状態」「セット」の列なし)。Excel 等で開いていて失敗したら dirty を立てる(次の変更で書き直す)"""
        self.mark()
        data = {"version": 1, "rows": list(self.rows.values()), "turns": self.turns}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols())
        for r in self.rows.values():
            if not r["failed"]:
                w.writerow([r[c] for c in cols()])
        try:
            write_atomic(self.store, json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            write_atomic(self.path(), buf.getvalue(), encoding="utf-8-sig")
            self.dirty = False
            return True
        except OSError as e:
            self.dirty = True
            self.on_error("ledger_save", e)
            return False

    # ---- 行
    def failed(self, rid) -> bool:
        return bool(self.rows[rid]["failed"])

    def add_measurement(self, no: str, mode: str, values: dict, failed: str = "", turn: int | None = None) -> str:
        """測定を1行足す(values = 計測ファイルの値)。failed(途中で止まった理由)があれば失敗の行。
        turn があれば、そのセットの何回目かを記録する。保存はしない(呼ぶ側が save)"""
        t = self.turn(turn)
        seq = len(t["recorded"]) + 1 if t else None
        if t:
            t["recorded"].append([no, seq])
        return self.insert({"No": no, "モード": mode, "日時": time.strftime("%Y-%m-%d %H:%M:%S"),
                            **values, "failed": failed, "turn": turn if t else None, "seq": seq})

    def set_failed(self, rids, reason: str) -> bool:
        """行を 有効(reason が空)/ 失敗 にする。終わっていないセットの行を含めば何もしない(False)"""
        if not rids or self.locked(rids):
            return False
        for i in rids:
            self.rows[i]["failed"] = reason
        return True

    def delete(self, rids) -> bool:
        if not rids or self.locked(rids):
            return False
        for i in rids:
            del self.rows[i]
        return True

    def set_cell(self, rid, name: str, value: str) -> bool:
        """セルの値を変える。変わったら True"""
        if rid not in self.rows or self.rows[rid][name] == value:
            return False
        self.rows[rid][name] = value
        return True

    # ---- セット
    def turn(self, tid) -> dict | None:
        return next((t for t in self.turns if t["id"] == tid), None)

    def begin_turn(self, values: dict, modes: list[str]) -> int:
        """「自動計測」: 新しいセットの記録を作る。開いているセットはいつも1つなので、前のセットは閉じる"""
        for t in self.turns:
            t["closed"] = True
        tid = max((t["id"] for t in self.turns), default=0) + 1
        self.turns.append({"id": tid, "start": time.strftime("%Y-%m-%d %H:%M:%S"), "keys": keys_of(values),
                           "plan": list(modes), "recorded": []})
        return tid

    def open_turn(self, values: dict) -> int | None:
        """開いているセット(再計測を待つ)が今の計測ファイルのものなら、その番号"""
        keys = keys_of(values)
        return next((t["id"] for t in self.turns if not t.get("closed") and t["keys"] == keys), None)

    def live_turn(self) -> dict | None:
        """開いているセット(いつも1つまで)"""
        return next((t for t in reversed(self.turns) if not t.get("closed")), None)

    def close_turn(self, tid) -> bool:
        """セットを閉じる。閉じたら True(閉じたセットは再計測の判定に使わない)"""
        if (t := self.turn(tid)) is not None and not t.get("closed"):
            t["closed"] = True
            return True
        return False

    def abort_turn(self, tid) -> int:
        """「終了」: 正常に終わらなかったセットの有効だった行を「セット中断」で無効にして閉じる(2026-11-17 ユーザーの判断:
        有効な行ができるのは、正常に終わったセットだけ)。無効にした行の数を返す"""
        rows = [r for r in self.rows.values() if r["turn"] == tid and not r["failed"]]
        for r in rows:
            r["failed"] = UI.LEDGER_FAILED_ABORT
        if (t := self.turn(tid)) is not None:
            t["closed"] = True
        return len(rows)

    def drop_turn(self, tid) -> bool:
        """照射が1回も無かったセットの記録を残さない(台帳の行が無いとき)。捨てたら True"""
        if (t := self.turn(tid)) is not None and not t["recorded"]:
            self.turns.remove(t)
            return True
        return False

    def turn_done(self, tid) -> tuple[int, int]:
        """セットの済み(有効な行と組みになった測定モードの並びの行)の数と、並びの行の数"""
        t = self.turn(tid)
        if t is None:
            return 0, 0
        st = self.pair(tid, t["plan"])
        return st.count("done"), len(st)

    def settle(self) -> bool:
        """読んだときに整える: 行の無いセットは捨て、済みが 0・全部済みのセットは閉じ、開いているのは最後の1つだけにする
        (「画面更新」の前にアプリを閉じた など)。変えたら True"""
        before = [(t["id"], t.get("closed")) for t in self.turns]
        self.turns = [t for t in self.turns if t["recorded"] or any(r["turn"] == t["id"] for r in self.rows.values())]
        opened = [t for t in self.turns if not t.get("closed")]
        for t in opened:
            done, n = self.turn_done(t["id"])
            if t is not opened[-1] or done == 0 or done >= n:
                t["closed"] = True
        return before != [(t["id"], t.get("closed")) for t in self.turns]

    def live_ids(self) -> set:
        """終わっていないセット(計測中・再計測や再開を待つ・「画面更新」の前)の番号"""
        return {t["id"] for t in self.turns if not t.get("closed")}   # 計測中のセットも閉じていない

    def locked(self, rids) -> bool:
        """終わっていないセットの行を含むか(編集・削除・有効 / 失敗の切り替えをしない)"""
        live = self.live_ids()
        return any(self.rows[i]["turn"] in live for i in rids if self.rows[i]["turn"] is not None)

    # ---- 判定
    def mark(self):
        """警告を付け直す: セットの記録と照らし合わせ、計測ファイル(Database・ID・subID)がセットと違う行は「不一致」、
        セットで入れた回が台帳から消えていれば、そのセットの残りの行は「欠け」"""
        self.warn = {}
        by_turn: dict[int, list[str]] = {}
        for rid, row in self.rows.items():
            if row["turn"] is not None:
                by_turn.setdefault(row["turn"], []).append(rid)
        for tid, rids in by_turn.items():
            t = self.turn(tid)
            if t is None:
                continue
            have = {self.rows[i]["seq"] for i in rids}
            gone = any(seq not in have for _, seq in t["recorded"])
            for i in rids:
                w = [k for k, on in (("mismatch", keys_of(self.rows[i]) != t["keys"]), ("missing", gone)) if on]
                if w:
                    self.warn[i] = w

    def shown(self, rid, live: set | None = None) -> tuple[str, str, str]:
        """表に出す (「状態」の文, 「セット」の文, 色の名前 warn / failed / live / "")。色は 警告 > 失敗 > セット中"""
        row = self.rows[rid]
        live = self.live_ids() if live is None else live
        on = row["turn"] is not None and row["turn"] in live
        state = "・".join([UI.LEDGER_LIVE] * on + [UI.LEDGER_WARN[k] for k in self.warn.get(rid, [])]
                         + [UI.LEDGER_FAILED.format(reason=row["failed"])] * bool(row["failed"]))
        turn = UI.LEDGER_TURN.format(turn=row["turn"], seq=row["seq"]) if row["turn"] is not None else ""
        tag = "warn" if rid in self.warn else "failed" if row["failed"] else "live" if on else ""
        return state, turn, tag

    def pair(self, tid, modes: list[str]) -> list[str]:
        """測定モード表の行(modes。上から)を、セット tid の台帳の行と同じモードどうしで組みにする: 有効な行と
        組みになれば "done"、失敗の行だけなら "fail"、どちらも無ければ ""(まだ測っていない)"""
        good, bad = Counter(), Counter()
        for row in self.rows.values():
            if row["turn"] == tid:
                (bad if row["failed"] else good)[row["モード"]] += 1
        out = []
        for m in modes:
            for c, st in ((good, "done"), (bad, "fail")):
                if c[m] > 0:
                    c[m] -= 1
                    out.append(st)
                    break
            else:
                out.append("")
        return out

    def measured(self, values: dict, modes: list[str]) -> bool:
        """今の計測ファイルで、測定モード表のモードの有効な行が台帳にあるか(計測済み。警告の有無は見ない)"""
        keys, modes = keys_of(values), set(modes)
        return any(not r["failed"] and r["モード"] in modes and keys_of(r) == keys for r in self.rows.values())

    def turn_warned(self, values: dict) -> bool:
        """今の計測ファイルの行に警告があるか(状態の欄に出す)"""
        keys = keys_of(values)
        return any(keys_of(self.rows[i]) == keys or self._turn_keys(i) == keys for i in self.warn)

    def _turn_keys(self, rid) -> list[str] | None:
        t = self.turn(self.rows[rid]["turn"])
        return t["keys"] if t else None
