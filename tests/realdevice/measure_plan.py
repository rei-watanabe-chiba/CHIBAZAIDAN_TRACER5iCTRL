# B1+B4: 計画を照射して測る。結果の行・失敗の判定・測定中の 0x19 の間隔を出す。最後に結果表の No を返す
import sys, time, json
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Traer5iAuto", "src"))
import auto_measure as am, device_link as dl, instrument_files as inf
plan = am.parse_plan(sys.argv[1]); set_each = "--no-set" not in sys.argv
alive, stages = [], []
orig = dl.DeviceLink._recv_exact
def rec(s, n):
    d = orig(s, n)
    if n == 4 and d == b"\x19\0\0\0":
        alive.append(time.time())
    return d
dl.DeviceLink._recv_exact = staticmethod(rec)
op = am.Operator()
link = dl.current_link() or dl.LINK
src = {"host": link.host, "source": link.source}
last_no = max(inf.file_no(r) or 0 for r in inf.results_rows(**src))
print("開始前の最後の No:", last_no)
rows = []
def on_result(r):
    rows.append(dict(r)); print("結果:", {k: r.get(k) for k in ("app", "set_s", "inst_time", "sample_no", "failed", "mismatch", "status")})
def on_stage(s): stages.append((time.time(), s))
t0 = time.time()
try:
    am.run_plan(op, plan, set_each=set_each, on_result=on_result, on_stage=on_stage,
                results_after=lambda no: [r for r in inf.results_rows(**src) if (inf.file_no(r) or 0) > no], last_no=last_no)
except Exception as e:
    print("例外:", type(e).__name__, e)
# 照射中の区間の 0x19 の間隔
iv = []
for i, (ts, s) in enumerate(stages):
    if s != "照射中": continue
    end = stages[i + 1][0] if i + 1 < len(stages) else time.time()
    a = [x for x in alive if ts <= x <= end]
    iv += [b - a_ for a_, b in zip(a, a[1:])]
if iv: print(f"照射中の 0x19: {len(iv)+1} 回・間隔 平均 {sum(iv)/len(iv):.2f} 秒・最大 {max(iv):.2f} 秒")
else: print("照射中の 0x19: 記録なし(全体", len(alive), "回)")
print("所要", round(time.time() - t0), "秒")
json.dump(rows, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
dl.close_link()
