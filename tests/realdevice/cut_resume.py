# B5: 照射中にソフトの側で接続を切り(_fail)、CUT_GAP 秒後につなぎ直して再開の流れを見る
import sys, time, threading
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Traer5iAuto", "src"))
import auto_measure as am, device_link as dl, instrument_files as inf
plan = am.parse_plan(sys.argv[1]); cut_after = float(sys.argv[2]); gap = float(sys.argv[3])
op = am.Operator(); dl.IN_USE = True
link = dl.LINK; host, source = link.host, link.source
src = {"host": host, "source": source}
last_no = max(inf.file_no(r) or 0 for r in inf.results_rows(**src))
print("開始前の最後の No:", last_no)
state = {}
def cutter():
    while "irr" not in state: time.sleep(0.1)
    time.sleep(cut_after)
    print(f"--- {cut_after:g} 秒照射したところで切断(ソフト)"); state["cut"] = time.time()
    dl.LINK._fail(ConnectionError("テスト: ソフトで切断"))
    time.sleep(gap)
    while True:
        try:
            dl.shared_link(connect=True, host=host, source=source); break
        except OSError as e:
            print("  つなぎ直し失敗:", e); time.sleep(2)
    print(f"--- つなぎ直した(切ってから {time.time() - state['cut']:.1f} 秒)")
def on_stage(s):
    if s == "照射中": state.setdefault("irr", time.time())
def on_result(r): print("結果:", {k: r.get(k) for k in ("app", "set_s", "inst_time", "sample_no", "failed", "mismatch", "status", "recovered")})
threading.Thread(target=cutter, daemon=True).start()
try:
    am.run_plan(op, plan, on_result=on_result, on_stage=on_stage, on_wait=lambda k: print("待ち:", k),
                results_after=lambda no: [r for r in inf.results_rows(**src) if (inf.file_no(r) or 0) > no], last_no=last_no)
except Exception as e:
    print("例外:", type(e).__name__, e)
print("最後の状態:", op.state())
dl.close_link()
