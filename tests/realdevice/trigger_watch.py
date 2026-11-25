"""機器のトリガーの状態(55101)と画面の状態を、変わるたびにミリ秒で出す(読むだけ。55100 の命令は送らない = 照射しない)。

    python tests/realdevice/trigger_watch.py [秒数(既定 60)]

測定のアプリ・RemoteCTRL・Instrument Tools は閉じておく(機器は同時に 1 つの相手としかつながらない)。
トリガーの状態: released = 照射できる / not_ready = 照射できない(トリガーロックなど)/ pulled = 照射中 / None = 知らせがまだ無い
"""
import os, sys, time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "Traer5iAuto", "src")
sys.path.insert(0, SRC)
sys.stdout.reconfigure(encoding="utf-8")

import auto_measure as am          # noqa: E402
import device_link as dl           # noqa: E402

total = float(sys.argv[1]) if len(sys.argv) > 1 else 60
found = dl.find_s1_titan()
if not found:
    sys.exit("機器が見つかりません(USB)")
dev = found[0]
link = dl.DeviceLink(dev["host"], source=dev.get("source"))
t0 = time.time()
stamp = lambda: f"{time.time() - t0:7.3f}s"
print(f"{stamp()} つなぐ: {dev['host']}(PC 側 {dev.get('source')})", flush=True)
link.connect()
print(f"{stamp()} つながった: 製造番号 {link.name}", flush=True)
op = am.Operator.__new__(am.Operator)       # 画面を読むためだけ(命令は送らない)
op.rc, op.ui = dl.DirectRemote(link), None
prev_status, prev_screen, last_read = object(), None, 0.0
try:
    while time.time() - t0 < total:
        if link.status != prev_status:
            prev_status = link.status
            print(f"{stamp()} トリガー: {link.status}", flush=True)
        if time.time() - last_read >= 1.0 and link.frame is not None:
            last_read = time.time()
            try:
                st = am.screen_state(op.rc.read_ui())
            except Exception as e:
                st = f"読めない({e})"
            if st != prev_screen:
                prev_screen = st
                print(f"{stamp()} 画面: {st}", flush=True)
        if link.error:
            print(f"{stamp()} 切れた: {link.error}", flush=True)
            break
        time.sleep(0.02)
finally:
    link.close()
    print(f"{stamp()} 閉じた", flush=True)
