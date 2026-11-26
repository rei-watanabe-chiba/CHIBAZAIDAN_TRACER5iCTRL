"""画面の決まり(画面を持たない): 機器の状態 → 状態の欄の文言・メインのボタン、入力の不備、未了の行と「再計測」/「再開」、
セットの終わり方、照射中の進みの文、アラートの文、窓の倍率と置き場所。
画面の側(controller.Controller)はこれを呼んで映すだけにする(2026-11-19: WebView2 へ作り替える前に、処理と画面を分けた)"""
from __future__ import annotations

from gui_config import READY_STATES, UI


# ---- 機器の状態
def device_status(info: dict, has_pass: bool) -> tuple[str, str]:
    """機器の状態の読み取り結果 -> (UI.DEVICE_STATUS のキー, 差し込む文字(アプリ名・画面名))"""
    if not info["ok"]:                             # つながっていない・切れた(OSError)/ それ以外で読めない
        return ("offline" if info.get("offline") else "error"), ""
    st = info["state"]
    if st == "result":                             # 結果画面: 結果を見たいことがあるので自動では戻さない(「画面更新」でメイン画面へ)
        return "result", ""
    if st in ("keypad", "measuring"):              # 「画面更新」では戻さない画面(手で閉じる)
        return "busy_manual", UI.SCREEN_NAME[st]
    if st in READY_STATES:                         # トリガーは引かれたままのときだけ出す
        return ("trigger" if info.get("trigger") == "release" else "ready"), info.get("app") or ""
    if st in ("login", "startup"):                 # 起動画面も「ログイン」ボタンでログインする
        return ("login" if has_pass else "login_nopass"), ""
    if st in ("booting", "safety"):                # 起動の途中 / 放射線の注意(数秒おきに自動で読み直す)
        return st, ""
    if st in UI.SCREEN_NAME:
        return "busy", UI.SCREEN_NAME[st]
    return "unknown", ""


def status_parts(key: str, x: str = "") -> tuple[str, str, str]:
    """(● の色, タイトル, 説明)。{x} にアプリ名・画面名を入れる"""
    color, title, desc = UI.DEVICE_STATUS[key]
    return color, title.format(x=x), desc.format(x=x)


def abort_parts(e: Exception) -> tuple[str, str]:
    """中断の例外 -> アラート欄の (タイトル, 説明)。接続の例外(ConnectionError・TimeoutError)は「接続切れ」、
    種類の無い例外は「機器エラー」"""
    kind = "offline" if isinstance(e, (ConnectionError, TimeoutError)) else getattr(e, "kind", "device")
    title, desc = UI.ABORT_TEXT.get(kind, UI.ABORT_TEXT["device"])
    return title, desc


def alert_parts(key: str, **kw) -> tuple[str, str]:
    """アラート欄の (タイトル, 説明)。文言は UI.ALERTS"""
    title, desc = UI.ALERTS[key]
    return title.format(**kw), desc.format(**kw)


def status_look(text: str, colors: tuple[str, str] | None = None) -> dict:
    """状態の欄の見た目: 地・字の色と記号。照射中は薄い赤で、字は 2 色で明滅(UI.IRRADIATING)。
    色は colors(地, 字)、無ければ文言ごとの UI.STATUS_COLORS。記号は UI.STATUS_TITLE_ICONS → 地の色ごとの UI.STATUS_ICONS"""
    ir = UI.IRRADIATING
    if text == ir["label"]:
        return {"bg": ir["bg"], "fg": ir["colors"][0], "icon": ir["icon"], "blink": list(ir["colors"]), "blink_ms": ir["blink_ms"]}
    bg, fg = colors or UI.STATUS_COLORS.get(text, UI.STATUS_COLORS["状態確認中"])
    return {"bg": bg, "fg": fg, "icon": UI.STATUS_TITLE_ICONS.get(text) or UI.STATUS_ICONS.get(bg, ""), "blink": None}


# ---- 入力の不備・台帳との組み合わせ
def missing(values: dict) -> list[str]:
    """空の必須項目"""
    return [k for k in UI.REQUIRED_FIELDS if not str(values.get(k, "") or "").strip()]


def input_issues(mode_issues: list[str], empty: list[str], open_states: dict | None, measured: bool,
                 lid: str = "closed") -> list[str]:
    """開始できない入力の不備(状態の欄に不備ごとに1行)。mode_issues: 測定モード表の不備 / empty: 空の必須項目 /
    open_states: 開いているセットと組みにした行の状態(開いているセットが無ければ None)/ measured: 同じ計測ファイル・モードの
    有効な行が台帳にあるか(開いているセットが無いときだけ見る)/ lid: 蓋の磁気センサーの状態(closed / open / none。
    閉じていなければ照射しない)。必須項目が空なら計測済みは調べない"""
    issues = [] if lid == "closed" else [UI.INPUT_ISSUES["lid_open" if lid == "open" else "lid_none"]]
    issues += list(mode_issues)
    if empty:
        issues.append(UI.INPUT_ISSUES["required"].format(fields="・".join(empty)))
    elif open_states is not None:
        if open_states and all(v == "done" for v in open_states.values()):
            issues.append(UI.INPUT_ISSUES["done"])    # 開いているセットの行がすべて済み
    elif measured:
        issues.append(UI.INPUT_ISSUES["done"])        # 同じ計測ファイル・モードの有効な行が台帳にある(subID を変えれば測れる)
    return issues


def issues_title(issues: list[str]) -> str:
    """入力の不備があるときの状態の欄のタイトル: 蓋が開いていれば「蓋なし」、センサーが無ければ「センサー未接続」、
    ほかは「入力エラー」"""
    if UI.INPUT_ISSUES["lid_open"] in issues:
        return UI.LBL_LID_OPEN
    return UI.LBL_LID_NONE if UI.INPUT_ISSUES["lid_none"] in issues else UI.LBL_INPUT_ERROR


def pending(st: dict) -> list:
    """再計測・再開で測る行(済みの行があり、済みでない行も残っているとき。それ以外は空 = 「自動計測」か計測済み)。
    st: 測定モード表の行 -> 状態(done / fail / "")"""
    left = [i for i, v in st.items() if v != "done"]
    return left if left and len(left) < len(st) else []


def resuming(st: dict) -> bool:
    """続きが「再開」か: 未了がまだ測っていない回(○)だけ。失敗の回(✕)があれば「再計測」(2026-11-17 ユーザーの判断)"""
    return all(st.get(i) != "fail" for i in pending(st))


def verb(st: dict) -> str:
    return UI.LBL_RESUME if resuming(st) else UI.LBL_REMEASURE


def main_button(key: str, left: bool, resume: bool, closing: bool) -> tuple[str, bool, bool]:
    """測定していないときのメインのボタン: (UI.MAIN_BUTTON のキー, 押せるか, 自動計測・再計測できる画面か)。
    key: 機器の状態 / left: 未了があるか / resume: 未了が「再開」か / closing: 「画面更新」で閉じるセットがあるか。
    結果画面で未了がある(停止の後など)は「再計測」「再開」(押すとメイン画面へ戻してから測る。2026-11-16 ユーザーの判断)。
    「画面更新」の前は、メイン画面でも「画面更新」(押すとセットを閉じる)"""
    startable = (key in UI.STARTABLE or (key == "result" and left)) and not closing
    if key == "checking":
        return "busy", False, startable                           # 状態を読んでいる間は押せない(文字は UI.BUSY_TEXT)
    if startable:
        return (("resume" if resume else "remeasure") if left else "start"), True, startable   # 入力の不備は呼ぶ側で
    if key in ("login", "login_nopass"):
        return "login", key == "login", startable                 # PASS が空なら押せない
    if key in ("busy", "busy_manual", "result", "unknown", "error") or key in UI.STARTABLE:   # STARTABLE は「画面更新」の前
        return "update", True, startable                          # 「画面更新」: 戻せる画面ならメイン画面へ戻して読み直す
    return "idle", False, startable                               # 未接続・起動中・安全確認待ち・手動動作中: 押せない「開始」


def run_states(rows: list, run: list, before: dict) -> dict:
    """計測を始めるときの行の状態: 測る行は待ち、ほかは前の状態(再計測・再開では済みの行を ✓ のまま)"""
    st = {i: before.get(i) or "wait" for i in rows}
    for i in run:
        st[i] = "wait"
    return st


def turn_end(shot: bool, done: int, total: int) -> str:
    """実行が終わったときのセットの終わり方(2026-11-17 ユーザーの判断): "drop" = 照射が無い(記録を残さない)/
    "close" = 済みが 0・全部済み(「画面更新」まで行の状態を出し、そこで閉じる)/ "open" = 一部済み(開いたまま「再計測」「再開」)"""
    if not shot:
        return "drop"
    return "close" if done == 0 or done >= total else "open"


def progress_text(step: str, elapsed: float, total: float) -> str:
    """照射中の状態の欄の説明(「2/4 Mudrock Air」改行「30 秒 ・ 残り 12 秒」)"""
    rest = f"残り {total - elapsed:.0f} 秒" if elapsed < total else f"終了待ち +{elapsed - total:.0f} 秒"
    return f"{step}\n{total:g} 秒 ・ {rest}"


def progress(i: int, n: int, elapsed: float, total: float) -> float:
    """全体の進み(0〜1。今の測定の分も含める)"""
    return (i - 1 + min(1.0, elapsed / total)) / n


# ---- 窓
MINI_OLD = {"status": [], "modes": ["modes"], "all": ["modes", "screen"]}   # 前の版の設定 mini_view(形の名前)


def mini_parts(saved) -> list[str]:
    """保存した小窓の中身(測定モード表・本体画面のうち入っているもの。UI.MINI_VIEWS の順)"""
    if isinstance(saved, str):
        return list(MINI_OLD.get(saved, []))
    return [v for v in UI.MINI_VIEWS if isinstance(saved, list) and v in saved]


def toggle_mini(now, part: str) -> list[str]:
    """小窓のボタンを押した後の中身: 「なし」は全部切る、ほかは押したものだけ入り切りする"""
    if part == UI.MINI_NONE:
        return []
    parts = set(mini_parts(now)) ^ {part}
    return [v for v in UI.MINI_VIEWS if v in parts]


def choose_zoom(work_h: int, dpi_k: float, fixed=None) -> float:
    """全体の倍率: fixed(設定の zoom)が UI.ZOOMS にあればそれ。無ければ、UI.DESIGN_HEIGHT が画面の高さに収まる一番大きい倍率"""
    if fixed in UI.ZOOMS:
        return fixed
    return next((z for z in UI.ZOOMS if UI.DESIGN_HEIGHT * dpi_k * z <= work_h), UI.ZOOMS[-1])


def place_in(area, saved, min_size, fraction):
    """窓の大きさと位置 (w, h, x, y) を決める。area: 画面の作業領域、saved: 前回の (w, h, x, y) か None。
    前回が無ければ作業領域の fraction の大きさで真ん中。前回があれば画面に収まるよう縮め、はみ出さない位置へ"""
    L, T, R, B = area
    W, H = R - L, B - T
    mw, mh = min(min_size[0], W), min(min_size[1], H)
    if saved:
        w, h, x, y = saved
    else:
        w, h = int(W * fraction[0]), int(H * fraction[1])
        x, y = L + (W - w) // 2, T + (H - h) // 2
    w, h = max(mw, min(w, W)), max(mh, min(h, H))
    x, y = min(max(x, L), R - w), min(max(y, T), B - h)
    return w, h, x, y
