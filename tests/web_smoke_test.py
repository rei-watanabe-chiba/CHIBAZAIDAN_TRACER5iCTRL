"""WebView2 版の確認(機器に触れない・X線は出ない・本物の設定と台帳とログを書き換えない)。TRACERexl で実行する

    python tests/web_smoke_test.py

窓を出して(1 分ほど)、画面に JavaScript を流したり画面のボタンを押したりして確かめ、閉じる。
機器はデモ版と同じ偽の機器(tests/demo_device.py の DemoDevice・DemoOperator。画面は tests/fake_device.py。127.0.0.1)だけ。
測定の流れ(auto_measure.run_plan)は本物を動かし、画面を読む・押す・トリガーの部分だけを偽の機器に置き換える。
結果は1行ずつ OK / NG。NG が1つでもあれば終了コード 1
"""
from __future__ import annotations

import ctypes, json, os, shutil, sys, tempfile, threading, time
from ctypes import wintypes as wt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "Traer5iAuto", "src"), os.path.join(ROOT, "tests")]
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import auto_measure as am                                   # noqa: E402
import controller as ctl_mod                                # noqa: E402
import demo_device as dg                                     # noqa: E402
import device_core as dc                                    # noqa: E402
import device_link as dl                                    # noqa: E402
import storage                                              # noqa: E402
import web_app as wa                                        # noqa: E402
from fake_device import FakeDevice                          # noqa: E402
from gui_config import UI                                   # noqa: E402

results: list[bool] = []


def hint_text(groups):
    """表の下の説明の文字だけ(札 + 文をつなげたもの。画面の textContent と同じ)"""
    return "".join((UI.HINT_TARGETS[t] if t else "") + "".join(x for _, x in items) for t, items in groups)


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return "rgb(%d, %d, %d)" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def check(name: str, cond, detail=""):
    results.append(bool(cond))
    print(f"{'OK' if cond else 'NG'}  {name}" + ("" if cond else f"  -> {detail}"))


def wait_for(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


_dpi = ctypes.c_int(-1)
ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(_dpi))
check("DPI: web_app を読み込むと、画面ごとの DPI に合わせる(2。窓の大きさ・小窓の配置は物理の px で扱う)", _dpi.value == 2, _dpi.value)

# ---------------------------------------------------------------- 本物の設定・台帳・ログを書き換えない
tmp = tempfile.mkdtemp(prefix="web_smoke_")
settings = os.path.join(tmp, "config", "gui_settings.json")
log_file = os.path.join(tmp, "config", "app_log.txt")
logs: list[str] = []
def _log(line):
    logs.append(line); storage.append_log(line, log_file)
wa.append_log = ctl_mod.append_log = _log
am.TRACER_DIR = os.path.join(tmp, "TracerData")
am.LEDGER_STORE = os.path.join(tmp, "config", "ledger.json")
am.LOG_CSV, am.ERROR_PNG = os.path.join(tmp, "config", "measure_log.csv"), os.path.join(tmp, "config", "last_error.png")
PLAN = [["Obsidian", [3]], ["Mudrock Air", [2, 2]]]
storage.save_settings({"plan": PLAN, "set_each": True, "pass": ""}, settings)

# ---------------------------------------------------------------- 偽の機器(デモ版と同じ。本物の機器には一切つながない)
for k in ("T_SELECT", "T_CONFIRM", "T_FIELD", "T_KEY", "T_BACK"):
    setattr(dg, k, 0.05)                                    # 画面の切り替えを速く
dev = dg.DemoDevice(PLAN)
dev.speed = 10
dev.fd = FakeDevice(screen=dg.image("main", dev.app)).start()
op = dg.DemoOperator(dev)
dg.install(dev, op)
FOUND = [{"host": "127.0.0.1", "source": None, "alias": "テスト機器"}]
dc.find_devices = lambda: list(FOUND) if dev.present else []
dl.find_s1_titan = lambda pending=False: list(FOUND)


def connect(host=None, source=None, refused_wait=None):
    assert host in (None, "127.0.0.1")                       # 本物の機器にはつながない
    lk = dl.LINK
    if lk is None or not lk.connected or lk.ports["screen"] != dev.fd.ports[0]:
        if lk is not None:
            lk.close(abort=True)
        lk = dl.DeviceLink("127.0.0.1", timeout=5, ports=dev.fd.ports)
        lk.connect()
        dl.LINK = lk
    return lk
dc.connect_link = connect
ctl_mod.results_rows = lambda **k: list(dev.records)
fetched = []
ctl_mod.fetch_measurements = lambda folder, nos, **k: (fetched.append(list(nos)), ([f"{n}.pdz" for n in nos] + ["Results.csv"], []))[1]
dl.NOTE = lambda *_: None
dl.AUTO_CONNECT = False

app = wa.WebApp(settings_path=settings)
show = am.progress
am.progress = lambda e, t: show(e * dev.speed, t)            # 照射の進みは速さに合わせる(デモ版と同じ)
C = app.ctl


def scenario(win):
    js = win.evaluate_js
    st = lambda k: js(f"JSON.stringify(app.state[{json.dumps(k)}] ?? null)")
    state = lambda k: json.loads(st(k))
    btn = lambda: (js("document.getElementById('btn-main').textContent"), not js("document.getElementById('btn-main').disabled"))
    title = lambda: (state("status") or {}).get("title")
    try:
        # ---- 起動時に WebView2 の溜めた画面のファイルを消す(消さないと前の版の js が動く)
        cache = os.path.join(tmp, "cache_check")
        for n in wa.CACHE_DIRS:
            os.makedirs(os.path.join(cache, "EBWebView", "Default", n, "x"), exist_ok=True)
        os.makedirs(os.path.join(cache, "EBWebView", "Default", "Local Storage"), exist_ok=True)
        wa.clear_cache(cache)
        check("起動: WebView2 の溜めたファイル(Cache・Code Cache・GPUCache)を消し、ほかは残す",
              os.listdir(os.path.join(cache, "EBWebView", "Default")) == ["Local Storage"])
        # ---- 土台(1 段目)
        check("窓: 画面を読み、Python から見た目の値を受け取った", wait_for(lambda: js("app.UI !== null && app.UI.LBL_LEDGER"), 20) == UI.LBL_LEDGER)
        check("見た目: 色は CSS の変数(gui_config の値そのまま)",
              js("getComputedStyle(document.documentElement).getPropertyValue('--SIDEBAR_BG').trim()") == UI.SIDEBAR_BG)
        check("F24: 蓋のセンサーの F24 の連打は画面のキーの処理へ渡さない(ほかのキーは渡す)",
              js("(() => { let n = []; const f = e => n.push(e.key); document.body.addEventListener('keydown', f);"
                 " for (const k of ['F24', 'F24', 'a']) document.body.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true}));"
                 " document.body.removeEventListener('keydown', f); return n.join(','); })()") == "a")
        check("見た目: 左の列の幅は UI.LEFT_WIDTH", js("document.getElementById('left').offsetWidth") == UI.LEFT_WIDTH,
              js("document.getElementById('left').offsetWidth"))
        for n in range(300):
            app.bridge.emit("test", n)
        check("やり取り: 300 件の知らせが順番どおりに届く", wait_for(lambda: js("app.stats.order.length") == 300)
              and js("app.stats.order.every((v, i) => v === i)"))
        names = []
        app.bridge.run(lambda: names.append(threading.current_thread().name))
        check("やり取り: 処理は画面の糸で行う", names == ["画面の糸"], names)

        # ---- 配置(左の列は固定の幅・本体画面は左の列の幅 − 余白・はみ出さない)
        sw = js("document.getElementById('screen').getBoundingClientRect().width")
        check("配置: 本体画面の幅は カードの幅 − 枠 − 左右の余白(縦横 3:4)",
              abs(sw - (UI.LEFT_WIDTH - 2 - UI.DEVICE_PAD * 2)) < 1
              and abs(js("document.getElementById('screen').getBoundingClientRect().height") - sw * 4 / 3) < 1.5, sw)
        check("配置: 左の列の中身は横にはみ出さない", js("document.getElementById('left').scrollWidth") <= UI.LEFT_WIDTH,
              js("document.getElementById('left').scrollWidth"))
        check("配置: ボタンは測定モード表の上・表は見出し + 3 行の高さ(行が少なくても)",
              js("document.getElementById('btn-main').getBoundingClientRect().bottom < document.getElementById('modes').getBoundingClientRect().top")
              and js("(m => Math.abs(m.offsetHeight - (m.querySelector('thead').offsetHeight + 26 * %d + 1)) <= 1)(document.getElementById('modes'))" % UI.MODE_ROWS)
              and UI.MODE_ROWS == 3)
        check("配置: 左の列は窓の高さに収まる(本体画面の下端が見える)",
              js("document.getElementById('screen').getBoundingClientRect().bottom <= innerHeight"),
              (js("document.getElementById('screen').getBoundingClientRect().bottom"), js("innerHeight"),
               js("(m => [m.offsetHeight, m.querySelector('thead').offsetHeight, [...m.querySelectorAll('tbody tr')].map(r => r.offsetHeight), getComputedStyle(document.documentElement).getPropertyValue('--row-h')])(document.getElementById('modes'))")))
        check("配置: サイドバーは窓の地に浮いた白い 1 枚のカード(四方に余白・右の列との間は COLUMN_GAP・右の列と上下がそろう・部品と本体画面の左右がそろう・カードに収まる)",
              js("""(() => { const r = id => document.getElementById(id).getBoundingClientRect(), b = r('left'), st = r('status'), sc = r('screen'), m = r('btn-main'), M = %d, G = %d;
                  return getComputedStyle(document.getElementById('left')).backgroundColor === 'rgb(255, 255, 255)'
                    && getComputedStyle(document.getElementById('sidebar')).backgroundColor === 'rgba(0, 0, 0, 0)'
                    && Math.abs(b.left - M) <= 1 && Math.abs(b.top - M) <= 1 && Math.abs(b.bottom - (innerHeight - M)) <= 1
                    && Math.abs(r('pane-top').top - b.top) <= 1 && Math.abs(r('pane-top').left - b.right - G) <= 1 && Math.abs(r('log').bottom - b.bottom) <= 1
                    && Math.abs(st.left - b.left - 1) <= 1 && Math.abs(st.right - b.right + 1) <= 1
                    && Math.abs(sc.left - m.left) <= 1 && Math.abs(sc.right - m.right) <= 1 && sc.bottom + 4 <= b.bottom; })()""" % (UI.MARGIN, UI.COLUMN_GAP)),
              js("document.getElementById('screen').getBoundingClientRect().bottom"))
        h_status = js("document.getElementById('status').offsetHeight")

        # ---- 起動したとき
        check("起動: 未接続(本体画面の「接続」)・ボタンは押せない「▶ 自動計測」",
              wait_for(lambda: title() == UI.DEVICE_STATUS["offline"][1]) and btn() == (UI.MAIN_BUTTON["start"][0], False), (title(), btn()))
        check("起動: 測定モード表は設定の計画(秒数・フェーズの欄の数)",
              [(r["app"], r["phases"]) for r in state("modes")["rows"]] == [("Obsidian", ["3"]), ("Mudrock Air", ["2", "2"])]
              and state("modes")["total"] == "測定 2 回 / 7 秒", state("modes"))

        # ---- 計測ファイル(3 段目)
        check("計測ファイル: 項目ごとに 1 行・必須項目に印(空は赤)・空なら欄の中に説明・バッジ「新規データ入力」",
              js("document.querySelectorAll('#info-card input').length") == len(UI.INFO_FIELDS)
              and js("document.querySelector('#info-card input[data-key=ID]').placeholder") == dict((k, d) for k, _l, d, *_ in UI.INFO_FIELDS)["ID"]
              and js("getComputedStyle(document.querySelector('#info-card input[data-key=ID]').parentNode.querySelector('.req')).backgroundColor")
              == "rgb(%d, %d, %d)" % tuple(int(UI.REQ_MARK_COLORS[0][i:i + 2], 16) for i in (1, 3, 5))
              and js("document.getElementById('info-badge').textContent") == UI.INFO_BADGE[0])
        js("(() => { const i = document.querySelector('#info-card input[data-key=項目2]'); i.value = 'メモ'; i.dispatchEvent(new Event('input')); })()")
        check("計測ファイル: 欄に打つと Python の値になる", wait_for(lambda: app.bridge.run(lambda: C.ledger.values["項目2"]) == "メモ"))

        # ---- 資料一覧(3 段目)
        csvp = os.path.join(tmp, "資料.csv")
        with open(csvp, "w", encoding="cp932", newline="") as f:
            f.write("資料ID,名称,地区\r\nA-002,黒曜石 b,北\r\nA-010,黒曜石 a,南\r\nA-001,黒曜石 c,北\r\n")
        app.api.refs_load(csvp)
        rows_shown = lambda: js("refsTable.keys.length")
        check("資料一覧: CSV を読み、全部の行・# は元の行番号・ID は「ID」を含む列・ファイル名",
              wait_for(lambda: rows_shown() == 3) and js("refsTable.get(0)[0]") == "2"
              and state("refs_view")["roles"] == {"ID": "資料ID"} and js("document.getElementById('refs-file').textContent") == "資料.csv")
        check("列の幅: 資料一覧も表の幅いっぱい・# の列には余りを配らない",
              js("(t => Math.abs(t.cols.reduce((a, c) => a + c.width, 0) - t.body.clientWidth) <= 1 && t.cols[0].width === t.cols[0].base)(refsTable)"),
              js("[refsTable.cols.map(c => [c.key, c.base, c.width]), refsTable.body.clientWidth]"))
        check("計測ファイル: 割り当てた列の名前を欄の下に出す", wait_for(lambda: js("[...document.querySelectorAll('.colnote')].filter(n => !n.hidden).map(n => n.textContent).join()") == "資料ID"))
        js("document.getElementById('refs-btn-filter').click()")
        js("(() => { const q = document.getElementById('refs-query'); q.value = '北'; q.dispatchEvent(new Event('input')); })()")
        check("資料一覧: 検索は打ち終わってから絞り込み、打つと ON(「検索」も青)", wait_for(lambda: rows_shown() == 2, 3)
              and state("refs_view")["on"] and js("document.getElementById('refs-btn-filter').classList.contains('on')"))
        js("document.getElementById('refs-on').click()")
        check("資料一覧: OFF で全件(文字列は残す)", wait_for(lambda: rows_shown() == 3) and js("document.getElementById('refs-query').value") == "北")
        js("[...document.querySelectorAll('#refs-table .vt-h')].find(h => h.textContent.startsWith('資料ID')).click()")
        check("資料一覧: 見出しのクリックで並べ替え(▲)・下の説明は並べ替えで変えない", wait_for(lambda: js("refsTable.keys.join()") == "2,0,1")
              and js("document.getElementById('refs-hint').textContent") == hint_text(UI.REF_HINT))
        js("pywebview.api.refs_assign('名称', '項目1')")
        check("資料一覧: 右クリックの項目指定(見出しにバッジ・計測ファイルの欄の下に列の名前)",
              wait_for(lambda: state("roles") == {"ID": "資料ID", "項目1": "名称"})
              and "項目1" in js("[...document.querySelectorAll('#refs-table .vt-h')].map(h => h.textContent).join()"))
        js("pywebview.api.refs_join('地区', '-', '資料ID')")
        check("資料一覧: 結合列は # の右(灰色)・「結合列」は青", wait_for(lambda: js("refsTable.cols[1].key") == "地区+資料ID")
              and js("refsTable.cols[1].cls") == "num" and js("document.getElementById('refs-btn-join').classList.contains('on')"))
        js("(() => { const b = document.querySelector('#refs-table .vt-body'); const r = [...b.querySelectorAll('.vt-row')][0];"
           "r.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); dispatchEvent(new MouseEvent('mouseup')); })()")
        check("資料一覧: 行のクリックで、割り当てた列の値を計測ファイルの欄へ",
              wait_for(lambda: app.bridge.run(lambda: (C.ledger.values["ID"], C.ledger.values["項目1"])) == ("A-001", "黒曜石 c"))
              and wait_for(lambda: js("document.querySelector('#info-card input[data-key=ID]').value") == "A-001"),
              app.bridge.run(lambda: dict(C.ledger.values)))
        check("設定: 資料一覧の CSV・割り当て・結合列を保存する",
              wait_for(lambda: (storage.load_settings(settings).get("refs") or {}).get("join") == ["地区", "-", "資料ID"])
              and storage.load_settings(settings)["refs"]["roles"] == {"ID": "資料ID", "項目1": "名称"}, storage.load_settings(settings).get("refs"))
        js("pywebview.api.refs_unjoin(); pywebview.api.set_values({ID: '', 項目1: '', 項目2: ''})")
        wait_for(lambda: js("refsTable.cols[1].key") == "資料ID")

        # ---- 段の境目
        h_top = js("document.getElementById('pane-top').offsetHeight")
        fit_top = "(c => c.offsetTop + c.offsetHeight - document.getElementById('info-col').offsetTop)(document.getElementById('info-card'))"
        check("段の境目: 1 段目は計測ファイルが収まる高さ以上", h_top >= js(fit_top) - 1, (h_top, js(fit_top)))
        js("(() => { const s = document.querySelector('.sash[data-i=\"0\"]'); const r = s.getBoundingClientRect();"
           "s.dispatchEvent(new MouseEvent('mousedown', {clientX: r.left + 5, clientY: r.top + 5, bubbles: true}));"
           "dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + 5, clientY: r.top + 85}));"
           "dispatchEvent(new MouseEvent('mouseup', {clientX: r.left + 5, clientY: r.top + 85})); })()")
        check("段の境目: ドラッグで高さを変え、割合を設定に保存する",
              wait_for(lambda: js("document.getElementById('pane-top').offsetHeight") > h_top + 10)
              and wait_for(lambda: isinstance(storage.load_settings(settings).get("right_split"), list)),
              (h_top, js("document.getElementById('pane-top').offsetHeight"), storage.load_settings(settings).get("right_split")))
        h_down = js("document.getElementById('pane-top').offsetHeight")
        js("(() => { const s = document.querySelector('.sash[data-i=\"0\"]'); const r = s.getBoundingClientRect();"
           "s.dispatchEvent(new MouseEvent('mousedown', {clientX: r.left + 5, clientY: r.top + 5, bubbles: true}));"
           "dispatchEvent(new MouseEvent('mousemove', {clientX: r.left + 5, clientY: r.top - 55}));"
           "dispatchEvent(new MouseEvent('mouseup', {clientX: r.left + 5, clientY: r.top - 55})); })()")
        check("段の境目: 上へドラッグすると 1 段目が縮む(計測ファイルが収まる高さまで)",
              wait_for(lambda: js("document.getElementById('pane-top').offsetHeight") < h_down - 10, 3)
              and js("document.getElementById('pane-top').offsetHeight") >= js(fit_top) - 1,
              (h_down, js("document.getElementById('pane-top').offsetHeight"), js("paneMins()"),
               js("(c => [c.scrollHeight, c.clientHeight, [...c.children].reduce((s, e) => s + e.offsetHeight, 0)])(document.getElementById('info-col'))")))

        # ---- 本体画面: 自動で見つけて確かめ、「接続可」で「接続」
        check("本体画面: USB の機器を見つけて確かめ、「接続可」・案内の文",
              wait_for(lambda: (state("device_list") or {}).get("can_connect"), 20)
              and state("device_list")["rows"][0]["state"] == UI.DEVICE_ROW_STATE["ready"]
              and state("device_list")["message"] == UI.DEVICE_READY, state("device_list"))
        js("document.getElementById('btn-connect').click()")
        check("本体画面: 「接続」で一覧を消して画面を出す", wait_for(lambda: js("document.getElementById('device-overlay').hidden")
              and js("app.frames") >= 1, 15) and dl.IN_USE)
        check("状態: つながったら読み、計測ファイルが空なら「入力エラー」(Database・ID)・ボタンは押せない",
              wait_for(lambda: title() == UI.LBL_INPUT_ERROR, 10) and not btn()[1]
              and UI.INPUT_ISSUES["required"].format(fields="Database・ID") in js("document.getElementById('status-detail').textContent"),
              (title(), btn(), js("document.getElementById('status-detail').textContent")))
        f0 = js("app.frames")
        js("(() => { const r = screen.getBoundingClientRect();"
           "screen.dispatchEvent(new MouseEvent('click', {clientX: r.left + r.width / 2, clientY: r.top + r.height / 4})); })()")
        check("本体画面: クリックは機器の px にして機器へ届く",
              wait_for(lambda: len(dev.fd.clicks) >= 2) and abs(dev.fd.clicks[0][1] - 240) <= 2 and abs(dev.fd.clicks[0][2] - 160) <= 2,
              dev.fd.clicks)

        # ---- 入力 → 準備OK
        win.evaluate_js("pywebview.api.set_values({Database: 'DB-T', ID: 'T-1', subID: '1'})")
        check("状態: 入力すると「準備OK」(アプリ名)・「▶ 自動計測」(変わった直後は少し押せない)",
              wait_for(lambda: title() == UI.DEVICE_STATUS["ready"][1]) and btn()[0] == UI.MAIN_BUTTON["start"][0], (title(), btn()))
        check("状態: 変わってから UI.MAIN_BUTTON_LOCK_MS たつと押せる", wait_for(lambda: btn()[1], 3), btn())
        check("状態の欄: 右に計測資料(Database・ID)", wait_for(lambda: "DB-T" in js("document.getElementById('aside').textContent")))
        check("状態の欄: 高さは文言で変わらない", js("document.getElementById('status').offsetHeight") == h_status,
              (h_status, js("document.getElementById('status').offsetHeight")))

        # ---- 測定モード表の操作
        rid = state("modes")["rows"][0]["id"]
        js(f"pywebview.api.mode_phases({json.dumps(rid)}, ['1.5'], true)")
        check("測定モード: 不正な値(整数でない)は表に入れず赤い文字で残し、入力の不備・ボタンは押せない",
              wait_for(lambda: state("modes")["rows"][0]["bad"] == [True] and state("modes")["rows"][0]["phases"] == ["1.5"])
              and wait_for(lambda: title() == UI.LBL_INPUT_ERROR) and not btn()[1], (state("modes"), title()))
        js(f"pywebview.api.mode_revert({json.dumps(rid)})")
        check("測定モード: Esc で表の値に戻ると直る", wait_for(lambda: state("modes")["rows"][0]["phases"] == ["3"])
              and wait_for(lambda: title() == UI.DEVICE_STATUS["ready"][1]))
        new = js("pywebview.api.mode_add(null)")
        time.sleep(0.3)
        new = app.bridge.run(lambda: C.modes.ids()[-1])
        check("測定モード: 「＋ 追加」で最後の行と同じモードを初期値の秒数で足す",
              [r["app"] for r in state("modes")["rows"]] == ["Obsidian", "Mudrock Air", "Mudrock Air"]
              and state("modes")["rows"][-1]["phases"] == ["30", "50"], state("modes"))
        js(f"pywebview.api.mode_set({json.dumps(new)}, 'Obsidian')")
        check("測定モード: モードを変えると秒数はそのモードの初期値", wait_for(lambda: state("modes")["rows"][-1]["phases"] == ["120"]))
        js(f"pywebview.api.mode_delete([{json.dumps(new)}])")
        check("測定モード: 削除", wait_for(lambda: len(state("modes")["rows"]) == 2))
        check("設定: 測定モード表を変えたら保存する", storage.load_settings(settings)["plan"] == [["Obsidian", [3.0]], ["Mudrock Air", [2.0, 2.0]]],
              storage.load_settings(settings).get("plan"))
        wait_for(lambda: btn()[1], 3)

        # ---- 照射前の確認(安全確認をチェックするまで押せない・キャンセル・Python の側でも確かめる)
        js("document.getElementById('btn-main').click()")
        check("照射前の確認: 「▶ 自動計測」で確認のダイアログ(「X線を 2 回照射します」)",
              wait_for(lambda: js("document.getElementById('confirm').open")) and
              js("document.querySelector('#confirm .lead').textContent") == UI.DANGER_LEAD.format(n=2))
        check("照射前の確認: 安全確認をチェックするまで「照射開始」は押せない", js("document.querySelector('#confirm .ok').disabled"))
        js("document.querySelector('#confirm .cancel').click()")
        time.sleep(0.5)
        check("照射前の確認: キャンセルなら照射しない", not C.running() and dev.trig == "pull" and not js("document.getElementById('confirm').open"))
        wait_for(lambda: btn()[1], 3)
        js("document.getElementById('btn-main').click()")
        wait_for(lambda: js("document.getElementById('confirm').open"))
        r = js("pywebview.api.confirm(true, [false])")
        time.sleep(0.5)
        check("照射前の確認: 安全確認のチェックが無い答えは、画面を通さずに来ても照射しない(Python の側で確かめる)",
              not C.running() and dev.trig == "pull")
        js("document.getElementById('confirm').close()")
        wait_for(lambda: btn()[1], 3)
        js("document.getElementById('btn-main').click()")
        wait_for(lambda: js("document.getElementById('confirm').open"))
        js("(() => { const c = document.querySelector('#confirm .checks input'); c.click(); })()")
        check("照射前の確認: チェックすると「照射開始」を押せる", not js("document.querySelector('#confirm .ok').disabled"))
        js("document.querySelector('#confirm .ok').click()")
        check("計測: 照射開始で計測が始まる・ボタンは「■ 停止」(照射までは押せない)・設定と表は変えられない",
              wait_for(lambda: C.running(), 5) and wait_for(lambda: btn()[0] == UI.MAIN_BUTTON["stop"][0]
              and js("document.getElementById('pass').disabled") and state("modes")["locked"], 5), (btn(), C.running()))
        check("計測: 照射中は「☢ 照射中」・停止を押せる・進捗バー・行は ▶",
              wait_for(lambda: title() == UI.IRRADIATING["label"], 20) and wait_for(lambda: btn()[1], 5)
              and not js("document.getElementById('status-bar').hidden")
              and any(r["no"] == UI.MODE_ROW_STATE["run"] for r in state("modes")["rows"]), (title(), btn(), state("modes")))
        check("計測: 照射中は本体画面を手で押せない", not app.device.manual and not js("manual"))
        check("計測: セット計測中は計測ファイルの欄を押せない・バッジ「セット計測中」・資料一覧からの転記もしない",
              js("document.querySelector('#info-card input[data-key=ID]').disabled")
              and js("document.getElementById('info-badge').textContent") == UI.INFO_BADGE_TURN[0]
              and not app.api.refs_use(0) and app.bridge.run(lambda: C.ledger.values["ID"]) == "T-1")
        check("計測: 台帳の今のセットの行は「⌛ セット計測中」で編集できない",
              wait_for(lambda: state("ledger")["rows"] and state("ledger")["rows"][-1]["locked"], 20)
              and UI.LEDGER_LIVE in state("ledger")["rows"][-1]["cells"][2]
              and not app.api.ledger_cell(state("ledger")["rows"][-1]["id"], "備考", "x"), state("ledger"))
        check("計測: 2 回とも終わると(ログに「完了」)台帳に 2 行・取り込み・行は ✓✓・ボタンは「↻ 画面更新」",
              wait_for(lambda: not C.running() and C.run_turn is None, 60) and any(l.startswith("完了(") for l in logs)
              and [rw["No"] for rw in C.ledger.book.rows.values()] == ["901", "902"] and fetched == [["901", "902"]]
              and wait_for(lambda: btn()[0] == UI.MAIN_BUTTON["update"][0], 5)
              and [r["no"] for r in state("modes")["rows"]] == [UI.MODE_ROW_STATE["done"]] * 2,
              (title(), btn(), C.ledger.book.rows, fetched, state("modes")))
        check("計測: 終わったら本体画面を手で押せる・設定を変えられる", app.device.manual and not js("document.getElementById('pass').disabled"))
        wait_for(lambda: btn() == (UI.MAIN_BUTTON["update"][0], True), 10)   # 画面のボタンが押せるようになってから
        js("document.getElementById('btn-main').click()")
        check("セットの終わり: 「画面更新」でメイン画面へ戻してセットを閉じ、ID・subID を空にする(入力エラー)",
              wait_for(lambda: title() == UI.LBL_INPUT_ERROR, 10) and dev.screen == "main"
              and C.ledger.current_values()["ID"] == "" and C.ledger.current_values()["Database"] == "DB-T"
              and C.ledger.book.live_turn() is None, (title(), dev.screen, C.ledger.current_values(), C.closing, C.btn_mode, C.btn_enabled, C.status_busy, C._queued, btn()))

        # ---- 計測台帳(3 段目): 閉じたセットの行はセルの編集・有効 / 失敗・削除ができる
        L = lambda: state("ledger")
        check("台帳: 計測した 2 行を出す(No・モード・セット)",
              [r["cells"][0] for r in L()["rows"]] == ["901", "902"] and L()["rows"][0]["cells"][3] == "S1-1" and not L()["rows"][0]["locked"],
              L()["rows"])
        r1 = L()["rows"][0]["id"]
        col = [c[0] for c in UI.LEDGER_COLS].index("備考")
        # 本物のマウスの知らせで押す(表は押した行を描き直すので、表の外のクリックと取り違えないこと)
        press = ("(() => { const c = document.querySelectorAll('#ledger-table .vt-body .vt-row')[%d].children[%d];"
                 " const o = {bubbles: true, button: 0, shiftKey: %s, ctrlKey: %s, detail: %d};"
                 " c.dispatchEvent(new MouseEvent('mousedown', o)); window.dispatchEvent(new MouseEvent('mouseup', o)); })()")
        js(press % (0, 0, "false", "false", 1))
        check("台帳: 行をクリックすると選ばれたまま", js("[...ledgerTable.sel]") == [r1], js("[...ledgerTable.sel]"))
        js(press % (1, 0, "true", "false", 1))
        check("台帳: Shift+クリックで複数選択", len(js("[...ledgerTable.sel]")) == 2, js("[...ledgerTable.sel]"))
        js(press % (0, 0, "false", "true", 1))
        check("台帳: Ctrl+クリックで 1 行外す", len(js("[...ledgerTable.sel]")) == 1)
        js("document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}))")
        check("台帳: 表の外のクリックで選択を外す", js("[...ledgerTable.sel]") == [])
        js(press % (0, col, "false", "false", 1))
        check("台帳: 1 回のクリックでは入力欄を出さない", js("!ledgerTable.editing"))
        js(press % (0, col, "false", "false", 2))
        check("台帳: 編集できる列のセルをダブルクリックすると入力欄", js("!!ledgerTable.editing && ledgerTable.editing.col") == col)
        js("ledgerTable.endEdit(false)")
        js(press % (0, 0, "false", "false", 2))
        check("表: 見出しは灰色の地で行と見分ける・説明は札(見出し / 行)と線の印",
              js("getComputedStyle(document.querySelector('#refs-table .vt-head')).backgroundColor") == rgb(UI.TABLE_HEAD_BG)
              and js("getComputedStyle(document.querySelector('#ledger-table .vt-head')).backgroundColor") == rgb(UI.TABLE_HEAD_BG)
              and js("[...document.querySelectorAll('#refs-hint .tag')].map(e => e.textContent).join()") == ",".join(UI.HINT_TARGETS[t] for t, _ in UI.REF_HINT if t)
              and js("document.querySelectorAll('#refs-hint .ico, #ledger-hint .ico').length") == sum(len(i) for _, i in UI.REF_HINT + UI.LBL_LEDGER_HINT)
              and js("document.getElementById('ledger-hint').textContent") == hint_text(UI.LBL_LEDGER_HINT)
              and "🔒" not in js("document.getElementById('ledger-hint').textContent"),
              (js("getComputedStyle(document.querySelector('#refs-table .vt-head')).backgroundColor"),
               js("document.getElementById('refs-hint').innerHTML.slice(0, 200)"), js("document.getElementById('ledger-hint').textContent")))
        check("表: 見出しのバッジは行の高さを埋めず文字の高さ",
              js("(b => b && b.getBoundingClientRect().height < b.parentElement.getBoundingClientRect().height - 8)(document.querySelector('#refs-table .vt-h .badge'))"))
        check("台帳: 編集できない列の見出しは線の鍵の印(絵文字の 🔒 は使わない)",
              js("document.querySelectorAll('#ledger-table .vt-h .lock').length") == sum(1 for c in UI.LEDGER_COLS if not c[1])
              and "🔒" not in js("document.getElementById('ledger-table').querySelector('.vt-head').textContent"))
        check("台帳: 鍵の印の列はダブルクリックしても入力欄を出さない", js("!ledgerTable.editing"))
        # 列の幅: 広ければ右に余白を残さない(見出しの長さの比)・狭ければ基準の幅のまま横にスクロール・ドラッグした幅を保存
        fill = "(t => [t.cols.reduce((a, c) => a + c.width, 0), t.body.clientWidth])(ledgerTable)"
        tw, cw = js(fill)
        check("列の幅: 台帳は表の幅いっぱい(右に余白なし)", abs(tw - cw) <= 1, (tw, cw))
        check("列の幅: 保存した幅が無ければ見出しの長さの比", js("(c => c[0].base < c[1].base && c[1].width / c[1].base > 1.01)"
                                                       "(ledgerTable.cols.filter(c => c.key === 'No' || c.key === 'Database'))"),
              js("ledgerTable.cols.map(c => [c.key, c.base, c.width])"))
        bases = js("ledgerTable.cols.map(c => c.base)")
        js("document.getElementById('ledger-table').style.maxWidth = '200px'; ledgerTable.render()")
        check("列の幅: 狭くすると基準の幅のまま(横にスクロール)",
              js("ledgerTable.cols.map(c => c.width)") == bases and js("ledgerTable.body.scrollWidth > ledgerTable.body.clientWidth"))
        js("document.getElementById('ledger-table').style.maxWidth = ''; ledgerTable.render()")
        js("(() => { const g = document.querySelectorAll('#ledger-table .vt-grip')[0], r = g.getBoundingClientRect();"
           " g.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0, clientX: r.x, clientY: r.y}));"
           " window.dispatchEvent(new MouseEvent('mousemove', {clientX: r.x + 30, clientY: r.y}));"
           " window.dispatchEvent(new MouseEvent('mouseup', {clientX: r.x + 30, clientY: r.y})); })()")
        tw, cw = js(fill)
        check("列の幅: ドラッグしても右に余白なし・幅を設定に保存",
              abs(tw - cw) <= 1 and wait_for(lambda: isinstance(storage.load_settings(app.settings_path).get("ledger_col_widths"), dict)),
              (tw, cw, storage.load_settings(app.settings_path).get("ledger_col_widths")))
        js(f"ledgerTable.sel = new Set([{json.dumps(r1)}]); ledgerTable.edit({json.dumps(r1)}, {col});"
           "ledgerTable.editing.inp.value = 'テストの備考';"
           "ledgerTable.editing.inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        check("台帳: セルをその場で編集して Enter で確定(保存する)",
              wait_for(lambda: app.bridge.run(lambda: C.ledger.book.rows[r1]["備考"]) == "テストの備考")
              and "テストの備考" in open(C.ledger.book.store, encoding="utf-8").read())
        check("台帳: 鍵の印の列は編集できない", not app.api.ledger_cell(r1, "No", "999"))
        app.api.ledger_failed([r1], True)
        check("台帳: 右クリックの「失敗にする」で薄い赤の行・File.csv に出さない",
              wait_for(lambda: L()["rows"][0]["tag"] == "failed")
              and "901" not in open(os.path.join(am.TRACER_DIR, UI.LEDGER_FILE), encoding="utf-8-sig").read())
        app.api.ledger_failed([r1], False)
        wait_for(lambda: L()["rows"][0]["tag"] == "")
        js(f"ledgerTable.sel = new Set([{json.dumps(r1)}]); deleteRows([{json.dumps(r1)}])")
        check("台帳: 削除は確かめてから(いいえなら消さない)", wait_for(lambda: js("document.getElementById('ask').open"))
              and js("document.getElementById('ask').querySelector('.no').click() || true") and len(L()["rows"]) == 2)
        check("台帳: 保存先のフォルダを見出しの行に出す", L()["folder"] == os.path.abspath(am.TRACER_DIR)
              and js("document.getElementById('ledger-path').textContent") == os.path.abspath(am.TRACER_DIR))

        # ---- 停止(照射中だけ)
        win.evaluate_js("pywebview.api.set_values({ID: 'T-2', subID: '1'})")
        wait_for(lambda: btn()[1] and btn()[0] == UI.MAIN_BUTTON["start"][0], 5)
        dev.speed = 1                                       # ゆっくり照射して停止を押す
        # ---- 小窓: 最小化すると同じ窓を小さくし、状態の欄とボタンだけ。常時前面がオンなら小窓で最小化しない・オフなら最小化。
        # 最大化で元の窓に戻す(小窓の位置を保存)。小窓のボタンで始めると、確かめる窓は元の窓に戻して出す
        u32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(app.window.native.Handle.ToInt64()))
        iconic = lambda: bool(u32.IsIconic(hwnd))
        zoomed = lambda: bool(u32.IsZoomed(hwnd))
        topmost = lambda: bool(u32.GetWindowLongW(hwnd, -20) & 8)
        shown = lambda sel: js(f"getComputedStyle(document.querySelector('{sel}')).display !== 'none'")
        size0 = (app.window.width, app.window.height)
        check("小窓: チェックの行に「最小化時に小窓にする」(既定はオン)", js("document.getElementById('mini-on').checked"))
        app.window.minimize()
        check("小窓: 最小化すると同じ窓が小窓になる(最小化しない・常時前面・状態の欄とボタンと「常時前面に表示する」だけ)",
              wait_for(lambda: app.compact and js("document.body.classList.contains('compact')"), 20)
              and wait_for(lambda: not iconic(), 5) and topmost() and app.window.width < size0[0] / 2
              and shown("#status") and shown("#btn-main") and shown("#mini-top") and not shown("#right")
              and not shown("#modes-pad") and not shown("#screen-wrap") and shown(".status-aside")
              and js("document.getElementById('mini-top').checked"),
              (app.compact, iconic(), topmost(), app.window.width, size0))
        check("小窓: 枠をドラッグして大きさを変えられない(枠は固定)", not (u32.GetWindowLongW(hwnd, -16) & 0x40000))
        min_box = lambda: bool(u32.GetWindowLongW(hwnd, -16) & 0x20000)   # WS_MINIMIZEBOX(窓を作り直すと hwnd が無効になり偽)
        max_box = lambda: bool(u32.GetWindowLongW(hwnd, -16) & 0x10000)   # WS_MAXIMIZEBOX
        check("小窓: 常時前面の間は最小化のボタンを押せない(最大化のボタンは押せる)", wait_for(lambda: not min_box(), 5) and max_box(),
              (min_box(), max_box()))
        time.sleep(0.8)
        r0 = wt.RECT(); u32.GetWindowRect(hwnd, ctypes.byref(r0))
        u32.MoveWindow(hwnd, r0.left + 100, r0.top + 80, 1600, 1200, True)   # Win + D の 2 回目のように外から大きさと位置を書き戻す
        r1 = wt.RECT()
        check("小窓: 外から大きさを変えられたら小窓の大きさと位置に戻す(Win + D を 2 回)",
              wait_for(lambda: u32.GetWindowRect(hwnd, ctypes.byref(r1)) and (r1.left, r1.top, r1.right, r1.bottom)
                       == (r0.left, r0.top, r0.right, r0.bottom), 5), ((r0.left, r0.top, r0.right, r0.bottom), (r1.left, r1.top, r1.right, r1.bottom)))
        app.window.minimize()
        check("小窓: 常時前面がオンなら、小窓で最小化を押しても小窓のまま", wait_for(lambda: not iconic() and app.compact, 5) and not iconic()
              and (time.sleep(1) or not iconic()))
        js("document.getElementById('mini-top').click()")
        check("小窓: 「常時前面に表示する」を外すと前面でなくなり、保存する",
              wait_for(lambda: storage.load_settings(settings).get("mini_on_top") is False, 5) and wait_for(lambda: not topmost(), 5))
        check("小窓: 常時前面を外すと最小化のボタンを押せる", wait_for(min_box, 5) and bool(u32.IsWindow(hwnd)), min_box())
        app.window.minimize()
        check("小窓: 常時前面がオフなら、小窓で最小化を押すと最小化", wait_for(iconic, 5) and app.compact)
        u32.ShowWindow(hwnd, 9)
        check("小窓: 最小化から戻すと小窓のまま", wait_for(lambda: not iconic() and app.compact and not app.minimized, 5),
              (iconic(), app.compact, app.minimized))
        js("document.getElementById('mini-top').click()")
        check("小窓: チェックし直すと常に前面で、最小化のボタンを押せない", wait_for(topmost, 5) and wait_for(lambda: not min_box(), 5), min_box())
        u32.SetWindowPos(hwnd, None, 120, 90, 0, 0, 0x0001 | 0x0004 | 0x0010)        # 小窓を動かす(NOSIZE・NOZORDER・NOACTIVATE)
        app.window.maximize()
        check("小窓: 最大化を押すと元の窓に戻し(大きさ・最大化でない)、小窓の位置を保存する",
              wait_for(lambda: not app.compact and not js("document.body.classList.contains('compact')"), 10)
              and wait_for(lambda: (app.window.width, app.window.height) == size0, 5) and not zoomed() and not topmost()
              and wait_for(lambda: storage.load_settings(settings).get("mini_pos") == [120, 90], 5),
              (app.compact, (app.window.width, app.window.height), size0, zoomed(), storage.load_settings(settings).get("mini_pos")))
        check("小窓: 元の窓に戻すと最小化のボタンを押せる", wait_for(min_box, 5), min_box())
        app.window.minimize()
        check("小窓: 次の小窓は保存した位置", wait_for(lambda: app.compact, 10)
              and wait_for(lambda: (app.window.native.Left, app.window.native.Top) == (120, 90), 5),
              (app.window.native.Left, app.window.native.Top))
        # ---- タイトルバーのボタン(WM_SYSCOMMAND)では、最大化・最小化を経由せずに出入りする(縮む・広がるアニメーションを出さない)
        seen = []
        u32.PostMessageW(hwnd, 0x112, 0xF030, 0)                # SC_MAXIMIZE = 最大化のボタン
        ok_full = wait_for(lambda: seen.append(zoomed()) or (not app.compact and not js("document.body.classList.contains('compact')")), 10)
        check("小窓: 最大化のボタンで、最大化を経由せずに元の窓(大きさ)へ戻る",
              ok_full and not any(seen) and wait_for(lambda: (app.window.width, app.window.height) == size0, 5),
              (ok_full, any(seen), (app.window.width, app.window.height), size0))
        seen = []
        u32.PostMessageW(hwnd, 0x112, 0xF020, 0)                # SC_MINIMIZE = 最小化のボタン
        ok_mini = wait_for(lambda: seen.append(iconic()) or app.compact, 10)
        check("小窓: 最小化のボタンで、最小化を経由せずに小窓になる(同じ位置)",
              ok_mini and not any(seen) and not iconic()
              and wait_for(lambda: (app.window.native.Left, app.window.native.Top) == (120, 90), 5),
              (ok_mini, any(seen), (app.window.native.Left, app.window.native.Top)))
        # ---- 小窓のボタン: モード・画面はそれぞれ入り切り、「なし」は両方を切る。高さは中身に合わせ、中身は次の小窓でも同じ
        mini_h = lambda: app.window.native.ClientSize.Height
        mini_pick = lambda v: js(f"document.querySelector('#mini-views [data-view={v}]').click()")
        mini_on = lambda: js("[...document.querySelectorAll('#mini-views .on')].map(b => b.dataset.view)")
        row_fits = lambda: js("(() => { const r = document.querySelector('.mini-row'), v = document.getElementById('mini-views'),"
                              " t = document.querySelector('.mini-row label'); return t.getBoundingClientRect().right <= v.getBoundingClientRect().left"
                              " && v.getBoundingClientRect().right <= r.getBoundingClientRect().right + 0.5; })()")
        fits = lambda: js("(() => { const s = document.getElementById('sidebar'); return s.scrollHeight <= s.clientHeight + 1; })()")
        def mini_to(*parts):
            return wait_for(lambda: storage.load_settings(settings).get("mini_view", []) == list(parts)
                            and mini_on() == (list(parts) or ["none"]), 5) and (time.sleep(0.6) or True)
        time.sleep(0.6)                              # 小窓になった直後の高さ合わせを待つ
        h0 = mini_h()
        mini_pick("modes")
        ok1 = mini_to("modes") and shown("#modes-pad") and not shown("#screen-wrap") and fits()
        h1 = mini_h()
        mini_pick("screen")
        ok2 = mini_to("modes", "screen") and shown("#modes-pad") and shown("#screen-wrap") and fits()
        h2 = mini_h()
        mini_flat = js("""(() => { const r = id => document.getElementById(id).getBoundingClientRect(), st = r('status'), sc = r('screen');
            return st.left === 0 && st.top === 0 && Math.abs(st.right - document.body.getBoundingClientRect().right) <= 1
              && getComputedStyle(document.getElementById('sidebar')).backgroundColor === 'rgb(255, 255, 255)'
              && getComputedStyle(document.getElementById('left')).borderTopWidth === '0px'
              && sc.bottom + 4 <= r('sidebar').bottom; })()""")
        mini_pick("modes")
        ok4 = mini_to("screen") and not shown("#modes-pad") and shown("#screen-wrap") and fits()
        h4 = mini_h()
        check("小窓: 「画面」だけ入れるとモード表は出さず、高さも本体画面の分だけ", ok4 and h0 < h4 < h2, (ok4, h0, h4, h2))
        mini_pick("none")
        ok3 = mini_to() and not shown("#modes-pad") and not shown("#screen-wrap") and fits()
        h3 = mini_h()
        check("小窓: 「モード」「画面」をそれぞれ入り切りでき、「なし」で両方消える。窓の高さが中身に合う",
              ok1 and ok2 and ok3 and h0 < h1 < h2 and abs(h3 - h0) <= 2, (ok1, ok2, ok3, h0, h1, h2, h3))
        check("小窓: カードだけ(周りの余白と枠なし)で、本体画面の下に余白がある", mini_flat)
        check("小窓: 切り替えは「常時前面に表示する」と重ならず、今の形が選ばれて見える", row_fits() and mini_on() == ["none"], (row_fits(), mini_on()))
        mini_pick("modes")
        mini_to("modes")
        app.window.maximize()
        wait_for(lambda: not app.compact, 10)
        app.window.minimize()
        check("小窓: 次の小窓も前の形(モード表)で、高さも合う",
              wait_for(lambda: app.compact, 10) and wait_for(lambda: abs(mini_h() - h1) <= 2, 5) and shown("#modes-pad"), (mini_h(), h1))
        mini_pick("none")
        mini_to()
        js("document.getElementById('btn-main').click()")
        check("小窓: ボタンで始めると元の窓に戻し、確かめる窓を出す",
              wait_for(lambda: js("document.getElementById('confirm').open"), 10) and wait_for(lambda: not app.compact, 10)
              and wait_for(lambda: (app.window.width, app.window.height) == size0, 5),
              (app.compact, (app.window.width, app.window.height)))
        js("document.getElementById('mini-on').click()")
        check("小窓: 「最小化時に小窓にする」を外すと保存する",
              wait_for(lambda: storage.load_settings(settings).get("mini_enabled") is False, 5))
        js("document.getElementById('mini-on').click()")
        js("document.querySelector('#confirm .checks input').click(); document.querySelector('#confirm .ok').click()")
        check("停止: 照射中になると押せる", wait_for(lambda: title() == UI.IRRADIATING["label"] and btn()[1], 20), (title(), btn()))
        js("document.getElementById('btn-main').click()")
        check("停止: トリガーを離して止まり、その回は「停止エラー」で台帳に・「停止しました」",
              wait_for(lambda: not C.running() and C.run_turn is None, 30) and dev.trig == "pull"
              and [r["failed"] for r in C.ledger.book.rows.values()][-1] == am.FAIL_STOP and any(l.startswith("停止しました(") for l in logs),
              (title(), [r["failed"] for r in C.ledger.book.rows.values()]))
        dev.speed = 10

        # ---- 閉じる(測定中: 照射を止めてから)
        wait_for(lambda: btn()[1], 10)
        js("document.getElementById('btn-main').click()")      # 画面更新でセットを閉じる
        wait_for(lambda: btn()[0] == UI.MAIN_BUTTON["start"][0] and btn()[1], 10)
        dev.speed = 1
        js("document.getElementById('btn-main').click()")
        wait_for(lambda: js("document.getElementById('confirm').open"))
        js("document.querySelector('#confirm .checks input').click(); document.querySelector('#confirm .ok').click()")
        wait_for(lambda: dev.trig == "release", 20)
        released = app.bridge.run(app.shutdown, timeout=40)
        check("閉じる: 照射中に閉じると照射を止め(トリガーを離す)、測定の処理を止め、機器との接続を切る",
              released and dev.trig == "pull" and not C.running() and dl.current_link() is None, (released, dev.trig, C.running()))
        check("閉じる: 照射の命令は偽の機器(デモ)だけで、本物の命令(TRIGGER_PULL)は一度も送っていない",
              "TRIGGER_PULL" not in dev.fd.commands if dev.fd else True)
        check("閉じる: 窓の大きさを設定に保存する", "web_zoomed" in storage.load_settings(settings))
    except Exception as e:
        import traceback
        check("思わぬエラー", False, traceback.format_exc())
        print("---- アプリのログ(最後の 30 行)\n" + "\n".join(logs[-30:]))
    finally:
        win.destroy()


try:
    app.start(scenario)
finally:
    dl.close_link()
    if dev.fd is not None:
        dev.fd.drop()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if results and all(results) else 1)
