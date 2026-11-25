# 実機: ログイン画面のまま resume_step が機器の結果表から回を決められるか(照射なし・トリガーの命令は送れないようにする)
import sys, time
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Traer5iAuto", "src"))
import device_link as dl, auto_measure as am, instrument_files as inf
def blocked(*a, **k): raise RuntimeError("BLOCKED: トリガーの命令は送らない")
dl.DeviceLink.command = blocked
am.Operator.trigger = blocked
op = am.Operator()
print("状態:", op.state())
link = dl.current_link() or dl.LINK
src = {"host": link.host, "source": link.source} if link else {}
rows = inf.results_rows(**src)
last = rows[-1]; n = inf.file_no(last)
print("結果表", len(rows), "行・最後:", last.get("File #"), last.get("Application"), last.get("ElapsedTime"))
after = lambda no: [r for r in inf.results_rows(**src) if (inf.file_no(r) or 0) > no]
app = last.get("Application")
am.RESULTS_RETRY = 1
cases = [("最後の1つ前から・同じモード", app, n - 1), ("最後から(新しい行なし)", app, n),
         ("最後の1つ前から・別のモード", "Obsidian" if app != "Obsidian" else "Mudrock Air", n - 1),
         ("2つ前から(2行)", app, n - 2)]
for name, a, no in cases:
    try:
        print(name, "→", am.resume_step(op, a, 30, time.time(), after, no))
    except Exception as e:
        print(name, "→", type(e).__name__, e)
dl.close_link()
