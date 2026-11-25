"""デモ(WebView2 版): 機器につながずに WebView2 版を動かして見る(開発用。配布しない)

    python tests/demo_web.py          (TRACERexl で実行)

- 偽の機器(tests/demo_device.py)(DemoDevice・DemoOperator)を使う。本物の機器には一切つながず、X線も出ない。
- 測定の流れ(auto_measure.run_plan)・WebView2 版の画面と中身(web_app・controller・device_core)は本物。
- 別の窓「デモの操作」で、接続エラー(USB を抜く・差す)・機器エラー・停止エラー・手の操作・ログアウトなどを起こせる。
- 設定・台帳・ログは一時フォルダに作り、閉じたら消す(本物の config / TracerData は読むだけで書き換えない)。
  測定モード表・計測情報・資料一覧の CSV の場所などは、本物の設定から写して始める(PASS は写さない)。
- 最初の「接続」はデモが押す。ログイン画面になったら、PASS に数字を入れて「ログイン」(どの数字でもよい)。
"""
from __future__ import annotations

import json, os, shutil, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "Traer5iAuto", "src"), os.path.dirname(os.path.abspath(__file__))]

import auto_measure as am                                   # noqa: E402
import controller as ctl_mod                                # noqa: E402
import demo_device as dg                                     # noqa: E402
import device_core as dc                                    # noqa: E402
import device_link as dl                                    # noqa: E402
import storage                                              # noqa: E402
import web_app as wa                                        # noqa: E402
from fake_device import FakeDevice                          # noqa: E402
from gui_config import UI                                   # noqa: E402

PANEL_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><style>
body { font: 13px "Yu Gothic UI", sans-serif; margin: 10px; background: #f3f3f3; }
fieldset { border: 1px solid #c8c8c8; margin: 6px 0; padding: 6px 10px; background: #fff; }
legend { color: #555; } label { display: block; margin: 2px 0; } button { margin: 2px 4px 2px 0; }
#state { white-space: pre; font-family: Consolas, "Yu Gothic UI"; margin-bottom: 6px; } .note { color: #555; margin-top: 6px; }
</style></head><body>
<div id="state"></div>
<fieldset><legend>次の照射で起こす</legend><div id="injects"></div>
  <label><input type="checkbox" id="keep"> 毎回起こす(外すと 1 回だけ)</label>
  <label>接続エラーで USB が抜けるのは照射の <input type="number" id="cut" min="5" max="95" step="5" style="width:4em"> %</label></fieldset>
<fieldset><legend>USB(接続)</legend><button data-do="pull_usb">USB を抜く</button><button data-do="plug_usb">USB を差す</button>
  <label><input type="checkbox" id="auto"> 抜けたら 3 秒後に自動で差す</label>
  <label><input type="checkbox" id="login_after"> 差したらログイン画面(結果画面が消える → 結果表で確かめる)</label></fieldset>
<fieldset><legend>本体を手で操作</legend><button data-do="logout">ログアウト(ログイン画面)</button><button data-do="main">メイン画面</button><br>
  <button data-do="pull">トリガーを手で引く</button><button data-do="release">手で離す</button><br>
  <button data-do="lid_open">蓋を開ける</button><button data-do="lid_close">蓋を閉じる</button><button data-do="lid_none">蓋のセンサーを抜く</button><br>
  <button data-do="settings">設定画面を開く</button><button data-do="keypad">テンキーを開く</button><button data-do="half">機器の時間を半分に</button></fieldset>
<fieldset><legend>速さ</legend><label>照射の速さ(倍) <input type="number" id="speed" min="1" max="60" style="width:4em"></label>
  <label><input type="checkbox" id="slow"> 画面の切り替えを遅く(停止を押しやすく)</label></fieldset>
<div class="note">停止は、アプリの「■ 停止」を好きなタイミングで押してください<br>(切替中・設定中・照射中・回と回の間・取得中)</div>
<script>
const api = () => pywebview.api, $ = id => document.getElementById(id);
window.addEventListener("pywebviewready", async () => {
  const s = await api().state();
  $("injects").innerHTML = s.injects.map(([k, t]) => `<label><input type="radio" name="inj" value="${k}"> ${t}</label>`).join("");
  for (const r of document.querySelectorAll("[name=inj]")) r.onchange = () => api().set("inject", r.value);
  for (const id of ["keep", "auto", "login_after", "slow"]) $(id).onchange = () => api().set(id, $(id).checked);
  for (const id of ["cut", "speed"]) $(id).onchange = () => api().set(id, +$(id).value);
  for (const b of document.querySelectorAll("[data-do]")) b.onclick = () => api().act(b.dataset.do);
  const show = async () => {
    const s = await api().state();
    $("state").textContent = s.text;
    for (const r of document.querySelectorAll("[name=inj]")) r.checked = r.value === s.inject;
    for (const id of ["keep", "auto", "login_after", "slow"]) if (document.activeElement !== $(id)) $(id).checked = s[id];
    for (const id of ["cut", "speed"]) if (document.activeElement !== $(id)) $(id).value = s[id];
  };
  show(); setInterval(show, 250);
});
</script></body></html>"""


class DemoApi:
    """「デモの操作」の窓から呼ぶもの(偽の機器の状態を変える)"""

    def __init__(self, dev: dg.DemoDevice):
        self._dev = dev

    def state(self):
        d = self._dev
        d.tick()
        trig = "照射中" if d.meas else "手で引いている" if d.trig == "release" else "離れている"
        times = d.times.get(d.app, [])
        text = (f"画面: {d.screen}    モード: {d.app}\nトリガー: {trig}    USB: {'あり' if d.present else '抜けている'}"
                f"\n機器の時間({d.app}): {am.fmt_phases(times)} 秒" + (f"\n照射 {d.elapsed():.0f} / {d.meas['dur']:g} 秒" if d.meas else "\n "))
        return {"text": text, "injects": dg.INJECTS, "inject": d.inject, "keep": d.keep_inject, "auto": d.auto_plug,
                "login_after": d.login_after, "slow": d.slow, "cut": d.cut_at, "speed": d.speed}

    def set(self, key, value):
        attr = {"keep": "keep_inject", "auto": "auto_plug", "inject": "inject", "login_after": "login_after",
                "slow": "slow", "cut": "cut_at", "speed": "speed"}[key]
        setattr(self._dev, attr, value)
        return True

    def act(self, what):
        d = self._dev
        if what == "pull_usb":
            threading.Thread(target=d.pull_usb, daemon=True).start()
        elif what == "plug_usb":
            threading.Thread(target=d.plug_usb, daemon=True).start()
        elif what == "logout":
            d.go("login")
        elif what in ("main", "settings", "keypad"):
            d.go(what)
        elif what == "pull" and d.trig == "pull" and not d.meas:
            d.trig = "release"; d._say("TRIGGER_PULLED")
        elif what == "release" and d.trig == "release" and not d.meas:
            d.trig = "pull"; d._say("TRIGGER_RELEASED")
        elif what in ("lid_open", "lid_close", "lid_none"):   # 偽の蓋のセンサー
            dg.LID.set({"lid_open": "open", "lid_close": "closed", "lid_none": "none"}[what])
        elif what == "half":
            for a, t in d.times.items():
                d.times[a] = [max(1.0, round(x / 2)) for x in t]
        return True


def build():
    """デモの WebView2 版を組み立てる(app, dev, tmp)。窓はまだ出さない"""
    tmp = tempfile.mkdtemp(prefix="traer5i_demo_web_")
    real = storage.load_settings(os.path.join(ROOT, "Traer5iAuto", "config", "gui_settings.json"))
    s = {k: real[k] for k in dg.KEEP if k in real}
    settings = os.path.join(tmp, "config", "gui_settings.json")
    storage.save_settings(s, settings)                       # 本物の設定・台帳・ログを書き換えない
    am.TRACER_DIR = os.path.join(tmp, "TracerData")
    am.LEDGER_STORE = os.path.join(tmp, "config", "ledger.json")
    am.LOG_CSV, am.ERROR_PNG = os.path.join(tmp, "config", "measure_log.csv"), os.path.join(tmp, "config", "last_error.png")
    log = os.path.join(tmp, "config", "app_log.txt")
    wa.append_log = ctl_mod.append_log = lambda line: storage.append_log(line, log)

    plan = [p for p in (s.get("plan") or UI.DEFAULT_PLAN) if p[0] not in UI.DISABLED_APPS]
    dev = dg.DemoDevice(plan)
    dev.fd = FakeDevice(screen=dg.image("main", dev.app)).start()
    dg.install(dev, dg.DemoOperator(dev))
    found = lambda: [{"host": "127.0.0.1", "source": None, "alias": dg.ALIAS}] if dev.present else []
    dc.find_devices = found
    dl.find_s1_titan = lambda pending=False: found()

    def connect(host=None, source=None, refused_wait=None):
        if not dev.present or dev.fd is None:
            raise ConnectionRefusedError("デモ: USB が抜けています")
        lk = dl.LINK
        if lk is None or not lk.connected or lk.ports["screen"] != dev.fd.ports[0]:
            if lk is not None:
                lk.close(abort=True)
            lk = dl.DeviceLink("127.0.0.1", timeout=5, ports=dev.fd.ports)
            lk.connect()
            dl.LINK = lk
        return lk
    dc.connect_link = connect
    ctl_mod.results_rows = lambda **k: list(dev.records)     # 機器の結果表(再開のときの確かめ)はデモの機器から

    def fetch(folder, nos, **k):                              # 取り込みのまね(取得中に停止を押せるか見られるよう 2 秒かける)
        time.sleep(2.0)
        return [f"{int(n):05d}-demo.pdz" for n in nos] + ["Results.csv"], []
    ctl_mod.fetch_measurements = fetch
    dl.AUTO_CONNECT = False

    app = wa.WebApp(settings_path=settings)
    show = am.progress                                       # 残り時間の表示は、照射の速さに合わせて進める
    am.progress = lambda e, t: show(e * dev.speed, t)
    return app, dev, tmp


def auto_connect(app: wa.WebApp):
    """最初の「接続」はデモが押す(接続可になったら)"""
    end = time.time() + 60
    while time.time() < end and not app.bridge.closed.is_set():
        d = app.bridge.run(app.device.chosen)
        if d and d.get("phase") == "ready":
            app.bridge.run(app.device.connect_found)
            return
        time.sleep(0.3)


def main():
    import webview
    app, dev, tmp = build()
    try:
        win = app.create_window()
        title = UI.WEB_TITLE + "  (デモ: 機器にはつながっていません・X線は出ません)"
        win.events.loaded += lambda: win.set_title(title)
        panel = webview.create_window("デモの操作(機器のまね)", html=PANEL_HTML, js_api=DemoApi(dev), width=440, height=640,
                                      resizable=False, on_top=False)
        win.events.closed += lambda: panel.destroy()
        panel.events.closing += lambda: app.bridge.closed.is_set()   # アプリを閉じるまで閉じない
        webview.start(lambda: auto_connect(app), private_mode=False,
                      storage_path=os.path.join(os.path.dirname(os.path.abspath(app.settings_path)), "webview"))
        app.bridge.stop()
    finally:
        if dev.fd is not None:
            dev.fd.drop()
        dl.close_link()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
