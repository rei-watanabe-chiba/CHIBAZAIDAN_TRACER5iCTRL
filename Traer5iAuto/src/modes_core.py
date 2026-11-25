"""測定モード表の中身の決まり(画面を持たない): フェーズの秒数の検査、モードの秒数の初期値、フェーズの数、保存した計画の読み方、
入力の不備、合計の文。画面の側(controller.Modes)はこれを呼ぶ(2026-11-19: WebView2 へ作り替える前に、処理と画面を分けた)"""
from __future__ import annotations

from gui_config import UI

PHASE_KEYS = [f"p{k}" for k in range(1, UI.MAX_PHASES + 1)]   # 行のフェーズの欄(p1〜p3)


def parse_phases(changed: dict) -> dict:
    """フェーズの秒数の入力を検査して整形する(changed にある欄だけ)。不正なら ValueError。
    出している欄(モードのフェーズの数)はどれも必須。秒数は整数(機器の設定画面が整数のみ)"""
    out = {}
    lo, hi, _ = UI.SEC_RANGE
    for k in PHASE_KEYS:
        if k not in changed:
            continue
        v = str(changed[k]).strip()
        try:
            sec = float(v)
        except ValueError:
            sec = None
        if sec is None or sec != int(sec) or not lo <= sec <= hi:
            raise ValueError(f"フェーズは整数で入力してください({lo}〜{hi} 秒)")
        out[k] = str(int(sec))
    return out


def phases_bad(values: dict) -> bool:
    """フェーズの欄の値(欄 -> 文字)に不正な値があるか"""
    try:
        parse_phases(values)
        return False
    except ValueError:
        return True


def default_times(app: str, phase_counts: dict, n: int | None = None) -> list[str]:
    """モードの秒数の初期値を欄の数(MAX_PHASES)ぶん。使わない欄は空。
    UI.DEFAULT_TIMES → 無ければフェーズ数で UI.DEFAULT_TIMES_BY_PHASES(1 = Obsidian、2 = Mudrock Air にならう)→ UI.DEFAULT_SEC"""
    n = n or phase_counts.get(app, 1)
    times = UI.DEFAULT_TIMES.get(app) or UI.DEFAULT_TIMES_BY_PHASES.get(n) or [UI.DEFAULT_SEC] * n
    return [str(times[i]) if i < min(n, len(times)) else "" for i in range(UI.MAX_PHASES)]


def phase_count(app: str, filled: int, phase_counts: dict) -> int:
    """行のフェーズ数(モードのフェーズ数。分からなければ入っている欄の数 filled)"""
    n = phase_counts.get(app) or filled or 1
    return max(1, min(UI.MAX_PHASES, n))


def saved_rows(saved: list) -> list[tuple[str, list[str]]]:
    """設定に保存した計画 [[モード, [秒, ...]], ...] を (モード, [秒の文字]) に。旧形式(秒数1つ / 回数付き)も読む。
    使わないモード(UI.DISABLED_APPS)の行は外す"""
    out = []
    for r in saved or []:
        if not (isinstance(r, (list, tuple)) and r) or r[0] in UI.DISABLED_APPS:
            continue
        app, phases, *_ = r
        phases = phases if isinstance(phases, list) else [phases]
        out.append((app, [str(round(float(x))) for x in phases]))
    return out


def issues(bad_rows: list[bool]) -> list[str]:
    """開始できない入力の不備(状態の欄に出す文。無ければ空): 行が無い / フェーズの欄に不正な値がある行がある。
    bad_rows: 行ごとに不正な値があるか"""
    if not bad_rows:
        return [UI.INPUT_ISSUES["no_modes"]]
    return [UI.INPUT_ISSUES["phase"]] if any(bad_rows) else []   # 不正な欄は赤い文字で分かる


def total_text(plan: list[tuple[str, list[float]]]) -> str:
    """ドックの右端の「測定 N 回 / S 秒」(秒数を足しただけ。切替の時間は入れない)"""
    return f"測定 {len(plan)} 回 / {sum(sum(p) for _, p in plan):g} 秒"
