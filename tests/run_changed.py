"""変えたファイルから、走らせるテストを選んで実行する(機器・本物の設定には触れない。各テストと同じ決まり)。

    python tests/run_changed.py            まだコミットしていない変更(未追跡のファイルも)から選ぶ
    python tests/run_changed.py main       main から今までの変更(コミット済みも)と、まだコミットしていない変更から選ぶ
    python tests/run_changed.py --all      全部走らせる(web は窓の操作も含めて = window)
    python tests/run_changed.py --list     選ぶだけで走らせない

選び方は CLAUDE.md の「テスト」の表と同じ(RULES)。どれにも当てはまらない src のファイルは core・logic・controller を走らせる。
資料(docs/・*.md)だけの変更では何も走らせない。結果は 1 つでも NG なら終了コード 1。
web は窓を後ろで開き(作業の邪魔をしない)、窓の操作(小窓・最大化・常時前面)を飛ばす。window は窓を前に出して全部を確かめる
(web の確認も含むので、両方が選ばれたら window だけ走らせる)。window を選ぶのは web_app.py を変えたときだけ。
"""
from __future__ import annotations

import os, re, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 走らせる順(速いものから)
TESTS = {
    "core": "tests/core_test.py",
    "logic": "tests/measure_logic_test.py",
    "controller": "tests/controller_test.py",
    "web": "tests/web_smoke_test.py",
    "window": "tests/web_smoke_test.py --window",
}
ALL = tuple(TESTS)
RETRY_ONCE = {"web", "window"}     # 本物の窓を動かすので、まれに間合いで外れる(2026-11-25: 段の境目・小窓の最大化で 1 回ずつ)。ほかは決まった結果なので走らせ直さない
SRC = r"Traer5iAuto/src/"

# (正規表現, テスト) を上から見て、最初に当てはまったものを使う
RULES = [
    (SRC + r"gui_config\.py$", ("core", "controller", "web")),
    (SRC + r"storage\.py$", ("core", "controller")),
    (SRC + r"(app_rules|\w+_core)\.py$", ("core",)),
    (SRC + r"(controller|device_core)\.py$", ("controller",)),
    (SRC + r"(auto_measure|device_link|lid_sensor)\.py$", ("logic", "controller")),
    (SRC + r"(screen_match|instrument_files|screen_ui|shutdown)\.py$", ("logic",)),
    (SRC + r"screens/", ("logic",)),
    (SRC + r"web_app\.py$", ("window",)),
    (SRC + r"web/", ("web",)),
    (SRC, ("core", "logic", "controller")),
    (r"tests/(core|measure_logic|controller|web_smoke)_test\.py$", None),   # そのテスト自身
    (r"tests/(demo_device|fake_device)\.py$", ("logic", "controller", "web")),
    (r"tests/fixtures/", ("logic",)),
    (r"requirements\.txt$", ALL),
]
SELF = {"core": "core", "measure_logic": "logic", "controller": "controller", "web_smoke": "web"}


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", creationflags=NO_WINDOW)
    if out.returncode:
        sys.exit(f"git {' '.join(args)} に失敗しました: {out.stderr.strip()}")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def changed(base: str | None) -> list[str]:
    files = set(git("diff", "--name-only", "HEAD"))                        # まだコミットしていない変更
    files |= set(git("ls-files", "--others", "--exclude-standard"))       # 未追跡のファイル
    if base:
        files |= set(git("diff", "--name-only", f"{base}...HEAD"))       # base から今までのコミット
    return sorted(f.replace("\\", "/") for f in files)


def pick(files: list[str]) -> dict[str, list[str]]:
    """テスト → それを選んだファイルの一覧"""
    why: dict[str, list[str]] = {}
    for f in files:
        for pat, tests in RULES:
            m = re.search(pat, f)
            if m:
                for t in tests or (SELF[m.group(1)],):
                    why.setdefault(t, []).append(f)
                break
    if "window" in why and "web" in why:                 # window は web の確認も含む
        why["window"] += why.pop("web")
    return {t: why[t] for t in ALL if t in why}


def run(name: str) -> bool:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, *TESTS[name].split()], cwd=ROOT, capture_output=True,
                         env=env, creationflags=NO_WINDOW)
    text = (out.stdout + out.stderr).decode("utf-8", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    summary = next((l for l in reversed(lines) if re.match(r"^\s*\d+/\d+", l)), lines[-1] if lines else "(出力なし)")
    ok = out.returncode == 0
    print(f"{'OK' if ok else 'NG'}  {name:<10} {summary.strip()}  ({time.monotonic() - t0:.0f} 秒)")
    if not ok:
        bad = [l for l in lines if l.lstrip().startswith("NG")]
        for l in (bad or lines[-15:])[:20]:
            print("      " + l)
    return ok


def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    base = next((a for a in argv if not a.startswith("--")), None)
    if "--all" in flags:
        chosen = {t: ["--all"] for t in ALL if t != "web"}
    else:
        files = changed(base)
        if not files:
            print("変更はありません。")
            return 0
        chosen = pick(files)
        if not chosen:
            print(f"変更 {len(files)} 件はテストの対象ではありません(資料など)。")
            return 0
    for t, why in chosen.items():
        shown = ", ".join(why[:3]) + (f" ほか {len(why) - 3} 件" if len(why) > 3 else "")
        print(f"→ {t:<10} {TESTS[t]}  ({shown})")
    if "--list" in flags:
        return 0
    print()
    results, shaky = [], []
    for t in chosen:
        ok = run(t)
        if not ok and t in RETRY_ONCE:              # 窓の操作の間合いでまれに外れる: 1 回だけ走らせ直す
            print(f"    {t} を 1 回だけ走らせ直します(窓の操作の間合いで外れることがあるため)")
            ok = run(t)
            if ok:
                shaky.append(t)
        results.append(ok)
    print(f"\n{sum(results)}/{len(results)} のテストが OK"
          + (f"(不安定: {', '.join(shaky)} は 1 回目 NG・2 回目 OK。上の NG の項目を覚えておく)" if shaky else ""))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
