"""測定の終了判定と時間の扱いの確認(機器に触れない・照射しない)

    python tests/measure_logic_test.py

機器の画面の移り変わり(測定中 → 結果画面)を仮想の時計で再現する偽の Operator を使い、
auto_measure.measure / run_plan が
  - 表の秒数ではなく「結果画面が出たか」で終了を判定すること
  - 機器の測定時間を結果画面の「時間 …」から読むこと
  - 表の時間とずれていたら検出すること
を確かめる。結果は1行ずつ OK / NG で表示。NG が1つでもあれば終了コード 1
"""
from __future__ import annotations

import os, sys, threading, time as _time, traceback

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Traer5iAuto", "src")   # テスト対象のコード
sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import auto_measure as am
import instrument_files as inf
from screen_ui import ScreenUI, UIElement

results: list[tuple[bool, str]] = []


def check(name: str, cond: bool, detail=""):
    results.append((bool(cond), name))
    print(f"{'OK' if cond else 'NG'}  {name}" + ("" if cond else f"  -> {detail}"))


class Clock:
    """仮想の時計。_sleep で進む(実際には待たない)"""
    t = 1000.0

    @classmethod
    def time(cls):
        return cls.t

    strftime = staticmethod(_time.strftime)
    sleep = staticmethod(lambda s: None)


def text(s):
    return UIElement("text", s, (0, 0, 10, 10))


class FakeOp:
    """機器の代わり。トリガー後 end_after 秒で結果画面になる(None なら終わらない)"""

    def __init__(self, end_after, result_texts):
        self.end_after, self.result_texts = end_after, result_texts
        self.trig, self.t_trigger, self.ui, self.polls = "pull", None, None, []

    def trigger_state(self):
        return self.trig

    def trigger(self):
        self.trig = "release" if self.trig == "pull" else "pull"
        if self.trig == "release" and self.t_trigger is None:
            self.t_trigger = Clock.t

    def _screen(self):
        done = self.end_after is not None and Clock.t - self.t_trigger >= self.end_after
        if done:
            return "result", ScreenUI(None, [], [text(s) for s in self.result_texts], 1.0)
        return "measuring", ScreenUI(None, [], [text("時間 12.3")], 1.0)    # 測定中も「時間」は出る

    def state(self, ui=None):
        st, self.ui = self._screen()
        self.polls.append(round(Clock.t - self.t_trigger, 1))
        return st

    def wait_state(self, want, timeout, interval=0.6):
        st, self.ui = self._screen()
        assert st in want, st
        return self.ui


class ResumeOp(FakeOp):
    """接続が切れる偽の機器。照射から drop_at 秒で切れ(画面を読むと ConnectionError)、relink でつながる。
    after: つながった後の画面("measuring" = 測定の続き / "login" = 結果画面が消えてログイン画面)"""

    def __init__(self, end_after, result_texts, drop_at=None, after="measuring"):
        super().__init__(end_after, result_texts)
        self.drop_at, self.after, self.off, self.dropped, self.logged_in, self.pulls = drop_at, after, False, False, False, 0
        self.rc = type("RC", (), {"relink": lambda _: (setattr(self, "off", False), True)[1]})()

    def trigger(self):
        if self.trig == "pull":
            self.pulls += 1
        super().trigger()

    def trigger_state(self):
        return "unknown" if self.off else self.trig

    def _screen(self):
        if self.off:
            raise ConnectionError("test: 切れた")
        if (self.t_trigger is not None and self.drop_at is not None and not self.dropped
                and Clock.t - self.t_trigger >= self.drop_at):
            self.dropped = self.off = True
            raise ConnectionError("test: 切れた")
        if self.dropped and self.after == "login":
            return ("main" if self.logged_in else "login"), ScreenUI(None, [], [], 1.0)
        if self.t_trigger is None:
            return "main", ScreenUI(None, [], [], 1.0)
        return super()._screen()

    def state(self, ui=None):
        st, self.ui = self._screen()
        return st


def resume_tests():
    """測定の途中で接続が切れたときの再開(auto_measure.run_plan の wait_reconnect・resume_step・ensure_ready)"""
    saved = (am.switch_app, am.set_phase_times, am.log, am.login, am.open_login)
    calls = []
    am.switch_app = lambda op, app: calls.append(("switch", app))
    am.set_phase_times = lambda op, app, ph: None
    am.log = lambda row: None
    am.login = lambda op, code: (calls.append(("login", code)), setattr(op, "logged_in", True))
    am.open_login = lambda op: None
    try:
        # 照射中に切れて、つなぎ直したらまだ測定中: 続きを待ち、結果画面から No と時間を読む(照射し直さない)
        op, rows, waits = ResumeOp(30, ["70800-01", "時間 30.0"], drop_at=10), [], []
        am.run_plan(op, [("Obsidian", [30])], set_each=False, on_result=rows.append, on_wait=waits.append,
                    results_after=lambda n: [], last_no=707)
        check("再開: 照射中に切れても中断せず、つなぎ直したら測定の続きを待って結果を読む(照射し直さない)",
              [r.get("sample_no") for r in rows] == ["708"] and rows[0].get("inst_time") == "30.0" and op.pulls == 1
              and waits == ["cut", "resumed"], (rows, op.pulls, waits))
        # 照射中に切れ、つなぎ直したら結果画面が消えてログイン画面: 機器の結果表から No と時間を読む。ログインは「再接続」で
        calls.clear()
        op, rows, waits = ResumeOp(30, [], drop_at=10, after="login"), [], []
        asked = []

        def after(n):
            asked.append(n)
            return [{"File #": "709", "Application": "Obsidian", "ElapsedTime": "26.0"}]
        stages = []
        try:
            am.run_plan(op, [("Obsidian", [30]), ("Mudrock Air", [30])], set_each=False, on_result=rows.append,
                        on_stage=stages.append,
                        on_wait=lambda k: (waits.append(k), k == "login" and am.request_login("1234")),
                        results_after=after, last_no=708)
            check("照射の途中で切れた: 停止と同じ扱いでターンを終える(CutStop)", False)
        except am.CutStop:
            pass
        check("照射の途中で切れた: 結果画面が消えていても機器の結果表から No を読み、その回は「接続エラー」(秒数は出さない)",
              [(r.get("sample_no"), r.get("failed")) for r in rows] == [("709", "接続エラー")] and asked == [708]
              and op.pulls == 1, (rows, asked, op.pulls))
        check("照射の途中で切れた: 測り直しを聞かず、次のモードへ進まない(ログインも待たない)。切れている間は「照射停止」、"
              "つないだ後は「照射中」でなく「結果確認中」",
              waits == ["cut", "resumed"] and ("switch", "Mudrock Air") not in calls and ("login", "1234") not in calls
              and stages[-1] == "結果確認中" and "照射中" in stages, (waits, calls, stages))
        # 結果画面が消えていて、結果表に新しい行も無い: 照射されていないとみなし、その回をはじめからやり直す
        calls.clear()
        op, rows = ResumeOp(30, ["71000-01", "時間 30.0"], drop_at=0, after="login"), []
        op.logged_in = True                              # つなぎ直したらメイン画面(結果画面は無い)
        real_screen = op._screen

        def screen_after_redo():
            if op.dropped and op.pulls >= 2:                # やり直しの照射の後は、ふつうに測定・結果画面
                op.after = "measuring"
            return real_screen()
        op._screen = screen_after_redo
        am.run_plan(op, [("Obsidian", [30])], set_each=False, on_result=rows.append,
                    results_after=lambda n: [], last_no=709)
        check("再開: 結果表に新しい行が無ければ照射されていないとみなし、その回をはじめからやり直す",
              op.pulls == 2 and calls.count(("switch", "Obsidian")) == 2 and [r.get("sample_no") for r in rows] == ["710"],
              (op.pulls, calls, rows))
        # 新しい行が2つ以上・アプリ名が違う: 確かめられないので機器エラーで中断
        op = ResumeOp(30, [], drop_at=10, after="login")
        try:
            am.run_plan(op, [("Obsidian", [30])], set_each=False,
                        results_after=lambda n: [{"File #": "711", "Application": "Mudrock Air", "ElapsedTime": "30.0"}],
                        last_no=710)
            check("再開: 結果表の新しい行がその回と合わなければ中断", False)
        except am.DeviceError as e:
            check("再開: 結果表の新しい行がその回と合わなければ(アプリ名が違う・2行以上)機器エラーで中断",
                  "確かめられません" in str(e), e)
        # 切替の途中で切れた(まだ照射していない): つなぎ直したら、その回をはじめからやり直す
        calls.clear()
        fail = [1]

        def flaky_switch(op, app):
            calls.append(("switch", app))
            if fail:
                fail.pop()
                raise ConnectionError("test: 切れた")
        am.switch_app = flaky_switch
        op, rows = ResumeOp(30, ["71200-01", "時間 30.0"]), []
        am.run_plan(op, [("Obsidian", [30])], set_each=False, on_result=rows.append, results_after=lambda n: [], last_no=711)
        check("再開: 照射の前(切替の途中)に切れたら、つなぎ直してその回をはじめから(照射は1回)",
              calls.count(("switch", "Obsidian")) == 2 and op.pulls == 1 and [r.get("sample_no") for r in rows] == ["712"],
              (calls, op.pulls, rows))
        # つなぎ直せない(relink が無い相手): 中断する
        am.switch_app = lambda op, app: (_ for _ in ()).throw(ConnectionError("test: 切れた"))
        try:
            am.run_plan(type("NoRelink", (), {"rc": object()})(), [("Obsidian", [30])], set_each=False)
            check("再開: つなぎ直せない相手なら中断", False)
        except ConnectionError:
            check("再開: つなぎ直せない相手(relink が無い)なら、待たずに「接続切れ」で中断", True)
    finally:
        am.switch_app, am.set_phase_times, am.log, am.login, am.open_login = saved


def run_measure(table_s, end_after, result_texts):
    op = FakeOp(end_after, result_texts)
    prog = []
    am.progress = lambda e, t: prog.append(round(e, 1))
    info = am.measure(op, table_s)
    return op, info, prog


class FakeInst:
    """機器(55000)の代わり。appear_after 回目の一覧で pdz が現れる"""

    def __init__(self, files, appear_after=0):
        self.files, self.appear_after, self.lists, self.gets = files, appear_after, 0, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def list(self, folder):
        self.lists += 1
        return [inf.Entry(n, len(d), None, False) for n, d in self.files.items()
                if n == inf.RESULTS or self.lists > self.appear_after]

    def get(self, remote):
        self.gets.append(remote)
        return self.files[remote.split("\\")[-1]]


def fetch_tests():
    """測定ごとのデータ取り込み(pdz は pdz フォルダへ、Results.csv は保存先へ上書き)"""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        files = {"00723-Obsidian.pdz": b"a", "00724-Mudrock Air.pdz": b"pdz724", inf.RESULTS: b"new"}
        with open(os.path.join(d, inf.RESULTS), "wb") as f:
            f.write(b"old")
        files["00725-Obsidian.pdz"] = b"pdz725"
        inst = FakeInst(files, appear_after=2)
        made = []
        real_inst = inf.Instrument
        inf.Instrument = lambda host, source=None: (made.append((host, source)), FakeInst(files))[1]   # 下の確認とは別の偽物
        try:
            inf.fetch_measurements(d, ["724"], wait=0, host="192.168.137.139", source="192.168.137.45")
            check("取込: 本体画面の接続のときに覚えた機器のアドレス・PC 側のアドレスでつなぐ",
                  made == [("192.168.137.139", "192.168.137.45")], made)
        finally:
            inf.Instrument = real_inst
        saved, missing = inf.fetch_measurements(d, ["724", "725"], wait=0, connect=lambda: inst)
        pdz = os.path.join(d, "pdz", "00724-Mudrock Air.pdz")
        check("取込: pdz は保存先の pdz フォルダへ(No で選ぶ)", saved[0] == pdz and open(pdz, "rb").read() == b"pdz724"
              and os.path.exists(os.path.join(d, "pdz", "00725-Obsidian.pdz")) and not missing, saved)
        check("取込: Results.csv は保存先へ上書き", open(os.path.join(d, inf.RESULTS), "rb").read() == b"new")
        check("取込: pdz が現れるまで一覧を確かめ直す", inst.lists == 3, inst.lists)
        check("取込: 他の No の pdz は取らない", not any("00723" in g for g in inst.gets), inst.gets)
        check("取込: Results.csv は最後に1回だけ取る", [g for g in inst.gets if g.endswith(inf.RESULTS)] == [inst.gets[-1]],
              inst.gets)
        check("取込: 一時ファイルを残さない", not any(n.endswith(".tmp") for _, _, fs in os.walk(d) for n in fs))
        saved, missing = inf.fetch_measurements(d, ["724", "999"], retries=2, wait=0, connect=lambda: FakeInst(files))
        check("取込: 機器に無い No は知らせて、ある分は取り込む", missing == ["999"] and len(saved) == 2, (saved, missing))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def detect_tests():
    """ボタン検出(Pillow だけ): 青い矩形を見つけ、文字の穴・小さいもの・矩形でないものを正しく扱う"""
    from PIL import Image, ImageDraw
    import ocr_read
    import screen_ui
    img = Image.new("RGB", (960, 600), (235, 235, 235))
    d = ImageDraw.Draw(img)
    blue = (20, 40, 200)
    d.rectangle((50, 50, 249, 109), fill=blue); d.text((80, 70), "OK", fill=(255, 255, 255))   # 文字入りのボタン
    d.rectangle((50, 40 + 140, 199, 40 + 140 + 59), fill=blue)
    d.rectangle((400, 400, 420, 415), fill=blue)                     # 小さすぎる
    d.line((600, 100, 900, 500), fill=blue, width=6)                 # 斜めの線(矩形でない)
    d.rectangle((300, 50, 499, 109), fill=blue); d.rectangle((300, 110, 340, 300), fill=blue)  # L字(面積が足りない)
    rects = screen_ui.detect_buttons(img)
    check("検出: 青いボタンを位置どおりに見つける(文字の穴は埋める)",
          rects[:1] == [(50, 50, 200, 60)] and (50, 180, 150, 60) in rects, rects)
    check("検出: 小さいもの・矩形でないものは除く", len(rects) == 2, rects)
    check("2値化(見本を作るときの OCR): 大津のしきい値は2つの山の間", 40 <= ocr_read._otsu([0] * 40 + [100] + [0] * 159 + [100] + [0] * 55) < 200)


def field_tests():
    """時間の欄の数値: 欄の中の数字(screen_match が字形で読んだ文字)を読む"""
    from PIL import Image
    img = Image.new("RGB", (960, 600), "white")
    f = UIElement("field", "", (361, 282, 207, 93))
    check("欄の値: 欄の中に数字が無ければ None", am.field_value(ScreenUI(img, [], [text("時間")], 1.0), f) is None)
    ui2 = ScreenUI(img, [], [UIElement("text", "120", (400, 300, 60, 40))], 1.0)
    check("欄の値: 欄の中の数字を読む", am.field_value(ui2, f) == 120.0)


def device_tests():
    """機器へ直接つなぐ(device_link)。偽の機器(fake_device)で確かめる。本物には触れない"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import device_link as dl
    from fake_device import FakeDevice, BUTTON
    fd = FakeDevice().start()
    link = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd.ports)
    try:
        link.connect()
        link.wait_frame(0, 3)                          # 最初の画面は connect の中では待たない
        check("直接: つなぐと機器の情報と最初の画面を受け取り、挨拶を返す",
              (link.width, link.height, link.name) == (480, 640, "FAKE001") and fd.hello == dl.HELLO
              and link.frame.getpixel((320, 150)) == (0, 0, 255) and link.frame.getpixel((10, 10)) == (255, 255, 255),
              (link.width, link.height, link.name, fd.hello))
        rc = dl.DirectRemote(link=link)
        check("直接: トリガーの状態は、まだ知らせが来ていなければ待ってから答える(すぐ測定を始めても「不明」にならない)",
              rc.trigger_state() == "pull" and link.status == "released", link.status)
        img = rc.capture_background()
        check("直接: 画像は機器の画面の縦横2倍(基準の 960 幅)", img.size == (960, 1280) and img.getpixel((640, 300)) == (0, 0, 255),
              img.size)
        check("直接: 基準座標(960 幅)→ 機器の座標", rc.to_client(None, (640, 300)) == (320, 150))
        seq = link.seq
        rc.click(320, 150)
        ok = link.wait_frame(seq, 3)
        check("直接: クリックは 押す → 離す の6バイトで届き、画面の更新を受け取る",
              ok and fd.clicks == [(2, 320, 150), (4, 320, 150)] and link.frame.getpixel((322, 152)) == (255, 0, 0), fd.clicks)
        check("直接: 照射できる状態は 'pull'", rc.trigger_state() == "pull")
        rc.trigger()
        check("直接: トリガー(照射開始)は 55100 に TRIGGER_PULL、照射中は 'release'",
              fd.commands == ["TRIGGER_PULL"] and rc.trigger_state() == "release", (fd.commands, link.status))
        rc.trigger()
        check("直接: 照射中のトリガーは TRIGGER_RELEASE で止め、照射できる状態に戻る",
              fd.commands == ["TRIGGER_PULL", "TRIGGER_RELEASE"] and link.wait_status(("released",), 3), (fd.commands, link.status))
        # 最初の画面が届いてから 55101・55100 をつなぐ(届く前につなぐと、機器はトリガーの状態の接続をすぐ切る。2026-11-12 実機)
        fd3 = FakeDevice(send_screen=False).start()
        link3 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd3.ports)
        saved_ff = dl.FIRST_FRAME_S
        try:
            dl.FIRST_FRAME_S = 0.5
            t0 = _time.time()
            try:
                link3.connect()
                check("つなぐ順番: 最初の画面が届かなければ失敗", False)
            except ConnectionError as e:
                _time.sleep(0.3)
                check("つなぐ順番: 最初の画面が FIRST_FRAME_S 届かなければ、55101・55100 はつながずに失敗(閉じる)",
                      "届きません" in str(e) and "status" not in fd3.socks and "command" not in fd3.socks
                      and not link3.connected and _time.time() - t0 < 2, (str(e), list(fd3.socks)))
        finally:
            dl.FIRST_FRAME_S = saved_ff
            link3.close(); fd3.drop()
        fd4 = FakeDevice().start()
        link4 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd4.ports)
        try:
            link4.connect()
            t0 = _time.time()
            while _time.time() - t0 < 2 and not ("status" in fd4.socks and "command" in fd4.socks):
                _time.sleep(0.02)
            check("つなぐ順番: 最初の画面が届いてから 55101・55100 を(裏で)つなぐ", link4.seq >= 1 and link4.connected
                  and "status" in fd4.socks and "command" in fd4.socks, (link4.seq, list(fd4.socks)))
        finally:
            link4.close(); fd4.drop()
        # トリガーの接続(55101)だけが機器に切られても、画面の接続は保ち、トリガーの接続だけをつなぎ直す
        fd5 = FakeDevice().start()
        link5 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd5.ports)
        saved_sr = dl.SUB_RETRY_S
        try:
            dl.SUB_RETRY_S = 0.3
            link5.connect()
            link5.wait_status(("released",), 3)
            fd5.cut("status")
            t0 = _time.time()
            while _time.time() - t0 < 2 and link5.status is not None:
                _time.sleep(0.02)
            check("トリガーの接続が切れる: 画面の接続は保ち(切れたとしない)、トリガーの状態は不明にする",
                  link5.connected and not link5.error and link5.status is None and "トリガーの状態" in link5.sub_error,
                  (link5.connected, link5.error, link5.status, link5.sub_error))
            ok = link5.wait_status(("released",), 3)
            check("トリガーの接続が切れる: 少し待ってトリガーの接続だけをつなぎ直し、状態の知らせがまた届く",
                  ok and link5.connected and not link5.sub_error, (link5.status, link5.sub_error))
        finally:
            dl.SUB_RETRY_S = saved_sr
            link5.close(); fd5.drop()
        # 切れたら3本ともすぐ捨て、つなぎ直すときは前と同じ PC 側のポート番号でつなぐ(機器が前の接続を片付けるように)
        fd9 = FakeDevice().start()
        link9 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd9.ports)
        try:
            link9.connect()
            link9.wait_status(("released",), 3)
            port9 = link9.local_port
            sub9 = {k: link9._socks[k].getsockname()[1] for k in ("status", "command")}
            fd9.cut("screen")
            t0 = _time.time()
            while _time.time() - t0 < 3 and not link9.error:
                _time.sleep(0.02)
            check("切れた: 理由を残し、3本ともすぐ捨てる(トリガーの状態は不明にする)",
                  bool(link9.error) and not link9._socks and link9.status is None and not link9.connected,
                  (link9.error, list(link9._socks), link9.status))
            for k in ("screen", "status", "command"):        # 偽の機器: もう一度受け付けるようにする
                threading.Thread(target=fd9._serve, args=(k,), daemon=True).start()
            link9.connect()
            t0 = _time.time()
            while _time.time() - t0 < 2 and not ("status" in link9._socks and "command" in link9._socks):
                _time.sleep(0.02)
            check("つなぎ直し: 前と同じ PC 側のポート番号で 55102 につなぐ",
                  link9.connected and link9.local_port == port9 and link9._socks["screen"].getsockname()[1] == port9
                  and link9.seq >= 1, (port9, link9.local_port))
            got9 = {k: link9._socks[k].getsockname()[1] for k in ("status", "command") if k in link9._socks}
            check("つなぎ直し: 55101・55100 も前と同じ PC 側のポート番号でつなぐ(機器に残った前の接続を片付けさせる)",
                  got9 == sub9 and link9.sub_ports == sub9, (sub9, got9))
            link9.booting = True
            link9.close(abort=True)
            for k in ("screen", "status", "command"):
                threading.Thread(target=fd9._serve, args=(k,), daemon=True).start()
            link9.connect()
            check("つなぎ直し: 初期化中の印(booting)はつなぎ直すと戻す", link9.connected and not link9.booting, link9.booting)
        finally:
            link9.close(); fd9.drop()
        # 機器の初期化の途中: 55101・55100 は断られても、画面の接続は保つ(裏で試し続ける)
        fd10 = FakeDevice().start()
        fd10._listen["status"].close(); fd10._listen["command"].close()   # 初期化の途中のまね: トリガーの接続は断る
        link10 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd10.ports)
        try:
            link10.connect()
            t0 = _time.time()
            while _time.time() - t0 < 4 and not link10.sub_error:
                _time.sleep(0.05)
            check("初期化の途中: 55101・55100 を断られても失敗にせず、画面の接続は保つ(トリガーの状態は不明)",
                  link10.connected and link10.seq >= 1 and "status" not in link10._socks and "command" not in link10._socks
                  and link10.sub_error, (link10.connected, list(link10._socks), link10.sub_error))
            import device_core as gd
            fd12 = FakeDevice().start()
            link12 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd12.ports)
            try:
                link12.connect()
                check("接続可の判定: 画面が届いていれば、55101 がまだ(初期化の途中)でも「接続可」(device_core.device_ready)",
                      gd.device_ready(link10) and gd.device_ready(link12))
            finally:
                link12.close(); fd12.drop()
        finally:
            link10.close(); fd10.drop()
        # 測定の再開(relink): 本体画面で「接続」した(IN_USE)、前と同じ機器(製造番号)の接続だけに付け替える
        fd11 = FakeDevice().start()
        link11 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd11.ports)
        saved_link, saved_use = dl.LINK, dl.IN_USE
        try:
            link11.connect()
            link11.wait_status(("released",), 3)
            rc11 = dl.DirectRemote(link=link11)
            dl.LINK = link11
            dl.IN_USE = False
            r1 = rc11.relink()
            dl.IN_USE = True
            rc11.serial = "900G00000"
            r2 = rc11.relink()
            rc11.serial = "FAKE001"
            r3 = rc11.relink()
            check("再開: 「接続」するまで(確かめている途中の接続)・別の機器(製造番号が違う)には付け替えず、同じ機器なら付け替える",
                  (r1, r2, r3) == (False, False, True), (r1, r2, r3))
        finally:
            dl.LINK, dl.IN_USE = saved_link, saved_use
            link11.close(); fd11.drop()
        # 送り元の固定: 決めた PC 側のアドレスからつなぐ(その接続だけを通る)
        fd6 = FakeDevice().start()
        link6 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd6.ports, source="127.0.0.1")
        try:
            link6.connect()
            check("送り元: 決めた PC 側のアドレス(その機器の USB の接続のもの)からつなぐ",
                  link6.connected and all(c.getsockname()[0] == "127.0.0.1" for c in link6._socks.values()),
                  [c.getsockname() for c in link6._socks.values()])
        finally:
            link6.close(); fd6.drop()
        # 機器を探す: 今有効な接続のうち、USB の Bruker(VID 226B)で、機器が「Bruker S1 Titan」と名乗るものだけ
        reg = {"A": (r"USB\VID_226B&PID_F001\X1", "イーサネット 4"), "B": (r"USB\VID_226B&PID_F001\X2", "イーサネット 5"),
               "C": (r"PCI\VEN_8086\Y", "Wi-Fi"), "D": (r"USB\VID_226B&PID_F001\X3", "イーサネット 6")}
        active = [{"id": "A", "ip": "192.168.137.45", "host": "192.168.137.139"},
                  {"id": "C", "ip": "192.168.30.196", "host": "192.168.30.254"},
                  {"id": "D", "ip": "192.168.137.46", "host": ""}]          # B は前に使った口(今は無い)。D はアドレスがまだ無い
        desc = {r"USB\VID_226B&PID_F001\X1": "Bruker S1 Titan - RNDIS", r"USB\VID_226B&PID_F001\X3": "Bruker S1 Titan - RNDIS"}

        class FakeReg:
            HKEY_LOCAL_MACHINE = 0
            @staticmethod
            def OpenKey(root, path):
                gid = path.split("\\")[-2]
                if gid not in reg:
                    raise OSError("無い")
                return gid
            @staticmethod
            def QueryValueEx(k, name):
                return (reg[k][0] if name == "PnPInstanceId" else reg[k][1], 1)
        real_reg, real_ad, real_desc = sys.modules.get("winreg"), dl._adapters, dl._bus_desc
        sys.modules["winreg"] = FakeReg
        dl._adapters = lambda: list(active)
        dl._bus_desc = lambda pnp: desc.get(pnp, "")
        try:
            got = dl.find_s1_titan()
            check("機器を探す: 今有効な、USB の Bruker S1 Titan の接続だけ。機器のアドレスはその接続の DHCP サーバー、"
                  "送り元はその接続の PC 側のアドレス(固定のアドレスは使わない)",
                  got == [{"host": "192.168.137.139", "source": "192.168.137.45", "alias": "イーサネット 4"}], got)
            got_p = dl.find_s1_titan(pending=True)
            check("機器を探す: pending なら、アドレスがまだ無い接続も返す(USB が現れた直後。host・source は空)",
                  got_p == [{"host": "192.168.137.139", "source": "192.168.137.45", "alias": "イーサネット 4"},
                            {"host": "", "source": "192.168.137.46", "alias": "イーサネット 6"}], got_p)
            desc[r"USB\VID_226B&PID_F001\X1"] = "Bruker Tracer 5 - RNDIS"
            check("機器を探す: 機器が「Bruker S1 Titan」と名乗らなければ拾わない", dl.find_s1_titan() == [])
        finally:
            if real_reg is not None:
                sys.modules["winreg"] = real_reg
            dl._adapters, dl._bus_desc = real_ad, real_desc
        # 本物の Windows の読み取り(機器にはつながない): 落ちずに一覧を返し、速い(PowerShell を使わない)
        t0 = _time.time()
        real = dl.find_s1_titan()
        check("機器を探す: Windows から直接読むので速い(1回 0.5 秒未満。PowerShell だと1回目は約 4 秒)",
              isinstance(real, list) and _time.time() - t0 < 0.5, round(_time.time() - t0, 3))
        # 生存の知らせ(約1秒ごと): 来ている間は切れない。黙って途絶えたら(エラーが返らない場合)ALIVE_TIMEOUT_S で切れたとみなす
        saved_to = dl.ALIVE_TIMEOUT_S
        dl.ALIVE_TIMEOUT_S = 2.5
        fd_main, link_main = fd, link
        fd = FakeDevice().start()                       # 別の偽の機器で(上の接続はこの後の確認で使う)
        link = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd.ports)
        link.connect()
        try:
            _time.sleep(3.5)
            check("生存: 知らせが届いている間は切れたとみなさない", link.connected and not link.error, link.error)
            fd.silent = True
            t0 = _time.time()
            while _time.time() - t0 < 6 and link.connected:
                _time.sleep(0.1)
            took = _time.time() - t0
            check("生存: 知らせが途絶えたら(通信は切れていなくても)約 ALIVE_TIMEOUT_S で切れたとみなし、接続を閉じる",
                  not link.connected and "届きません" in link.error and took < 5, (round(took, 1), link.error))
            try:
                link.command("TRIGGER_RELEASE", timeout=0.5)
                check("生存: 切れたとみなした後の命令は接続切れ", False)
            except ConnectionError:
                check("生存: 切れたとみなした後の命令は接続切れ(ConnectionError)", True)
        finally:
            dl.ALIVE_TIMEOUT_S = saved_to
            link.close(); fd.drop()
            fd, link = fd_main, link_main
        # トリガーの接続(55101・55100)は返事を待つ上限が短い。返事が無いときも、断られたときと同じ短い間隔で試し直す
        fdt = FakeDevice().start()
        lkt = dl.DeviceLink("127.0.0.1", timeout=5, ports=fdt.ports)
        seen_to, fails, real_open = [], {"status": 2}, lkt._open

        def open_sub_test(port, local=0, timeout=dl.CONNECT_TIMEOUT_S):
            if port == fdt.ports[1]:
                seen_to.append(timeout)
                if fails["status"] > 0:
                    fails["status"] -= 1
                    raise TimeoutError("test: 返事が無い")
            return real_open(port, local, timeout)
        lkt._open = open_sub_test
        try:
            t0 = _time.time()
            lkt.connect()
            while _time.time() - t0 < 8 and "status" not in lkt._socks:
                _time.sleep(0.05)
            took = _time.time() - t0
            check("トリガーの接続: 返事を待つ上限は SUB_CONNECT_TIMEOUT_S(画面の接続より短い)、返事が無くても 1 秒おきに試し直す",
                  "status" in lkt._socks and seen_to and all(t == dl.SUB_CONNECT_TIMEOUT_S for t in seen_to)
                  and dl.SUB_CONNECT_TIMEOUT_S < dl.CONNECT_TIMEOUT_S and took < 2 * dl.SUB_REFUSED_RETRY_S + 1.5,
                  (seen_to, round(took, 1)))
        finally:
            lkt.close(); fdt.drop()
        # 初期化中(画面が「初期化中」で booting を立てた): 初期化の終わりに機器は約 7 秒黙り、同じ接続のまままた送ってくるので切らない
        fd_main, link_main = fd, link
        fd = FakeDevice().start()
        link = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd.ports)
        link.connect()
        saved_to, saved_boot = dl.ALIVE_TIMEOUT_S, dl.BOOT_ALIVE_TIMEOUT_S
        dl.ALIVE_TIMEOUT_S, dl.BOOT_ALIVE_TIMEOUT_S = 2.5, 6.0
        notes, saved_note = [], dl.NOTE
        dl.NOTE = notes.append
        try:
            link.booting = True
            fd.silent = True
            _time.sleep(4.0)
            check("生存: 初期化中は、ふだんの上限(ALIVE_TIMEOUT_S)を過ぎて黙っても切らない",
                  link.connected and not link.error, link.error)
            fd.silent = False
            _time.sleep(1.5)
            check("生存: 初期化中に黙った後、同じ接続でまた届けばそのまま使い、止まっていた長さをログに残す",
                  link.connected and any("同じ接続でまた届いた" in n for n in notes), notes)
            fd.silent = True
            t0 = _time.time()
            while _time.time() - t0 < 9 and link.connected:
                _time.sleep(0.05)
            took = _time.time() - t0
            check("生存: 初期化中でも BOOT_ALIVE_TIMEOUT_S 黙ったら切れたとみなす",
                  not link.connected and "初期化中" in link.error and took > 4.0, (round(took, 1), link.error))
        finally:
            dl.ALIVE_TIMEOUT_S, dl.BOOT_ALIVE_TIMEOUT_S, dl.NOTE = saved_to, saved_boot, saved_note
            link.close(); fd.drop()
            fd, link = fd_main, link_main
        fd.say("TRIGGER_NOT_READY"); link.wait_status(("not_ready",), 3)
        try:
            link.pull()
            check("直接: 照射できない状態(ログイン画面など)では照射の命令を送らない", False, "送った")
        except RuntimeError:
            check("直接: 照射できない状態(ログイン画面など)では照射の命令を送らない",
                  fd.commands == ["TRIGGER_PULL", "TRIGGER_RELEASE"] and rc.trigger_state() == "unknown")
        check("直接: 照射していなければ release は何もしない", link.release() and fd.commands.count("TRIGGER_RELEASE") == 1)
        # Operator から使う
        kept = []
        real_keep = dl._keep_new_screen
        dl._keep_new_screen = lambda raw: kept.append(raw.size)
        dl.LINK = link
        try:
            op = am.Operator()
            fd.say("TRIGGER_RELEASED"); link.wait_status(("released",), 3)
            ui = op.read()
            check("直接: Operator が画面を読み、ボタンを見つける", len(ui.buttons) == 1 and ui.buttons[0].rect[0] == BUTTON[0] * 2,
                  [b.rect for b in ui.buttons])
            check("直接: Operator のトリガーの状態", op.trigger_state() == "pull")
            n = len(fd.clicks)
            op.click_elem(ui, ui.buttons[0], wait=0.3)
            for _ in range(30):                            # この試験の間 am.time は仮想の時計なので、離すのが届くまで待つ
                if len(fd.clicks) >= n + 2:
                    break
                _time.sleep(0.05)
            check("直接: Operator のクリックはボタンの中心(機器の座標)", fd.clicks[n:] == [(2, 320, 150), (4, 320, 150)], fd.clicks[n:])
            check("直接: 見本に無い画面は OCR を使わず「不明」とし、見本の候補として残す", kept and kept[0] == (480, 640)
                  and am.Operator.state(None, ui) == "unknown", kept)
        finally:
            dl.LINK = None
            dl._keep_new_screen = real_keep
        # 見本にある画面(実機のメイン画面)は照合で読む
        from PIL import Image
        fd2 = FakeDevice(screen=Image.open(os.path.join(ROOT, "screens", "main.png"))).start()
        link2 = dl.DeviceLink("127.0.0.1", timeout=5, ports=fd2.ports)
        try:
            link2.connect()
            rc2 = dl.DirectRemote(link=link2)
            ui = rc2.read_ui()
            check("直接: 見本にある画面は照合で読む",
                  am.Operator.state(None, ui) == "main" and am.Operator.current_app(None, ui) == "Mudrock Air")
        finally:
            link2.close(); fd2.drop()
        fd.drop()
        _time.sleep(0.5)
        try:
            rc.capture_background()
            check("直接: 機器が切れたら、読み取りはエラーにする", False, "エラーにならない")
        except ConnectionError:
            check("直接: 機器が切れたら、読み取りはエラーにする", not link.connected)
    finally:
        link.close()
        fd.drop()


def match_tests():
    """見本との照合(screen_match)。集めた実機の画面を読み、状態・アプリ・時間の値が正しいか。OCR は使わない"""
    import screen_match as sm
    from PIL import Image
    lib = sm.library()
    folder = os.path.join(ROOT, "screens")
    want = {   # 画面: (状態, アプリ, 時間の欄の値)
        "login": ("login", "", []), "main": ("main", "Mudrock Air", []), "main_Obsidian": ("main", "Obsidian", []),
        "main_Glass_Dual": ("main", "Glass Dual", []), "main_Precious_Metals_2": ("main", "Precious Metals 2", []),
        "main_Spectrometer_Mode": ("main", "Spectrometer Mode", []), "select_app": ("select_app", "", []),
        "confirm": ("confirm", None, []), "keypad": ("keypad", "", []),
        "settings_Obsidian": ("settings", "", [15.0]), "settings_Glass_Dual": ("settings", "", [15.0, 60.0]),
        "settings_Mudrock_Air": ("settings", "", [15.0, 15.0]), "settings_Precious_Metals_2": ("settings", "", [15.0]),
        "settings_Spectrometer_Mode": ("settings", "", [30.0]),
    }
    bad = []
    for name, (st, app, fields) in want.items():
        ui = lib.read(Image.open(os.path.join(folder, name + ".png")))
        got = (am.Operator.state(None, ui), am.Operator.current_app(None, ui), [am.field_value(ui, f) for f in am.time_fields(ui)])
        if got[0] != st or (app is not None and got[1] != app) or got[2] != fields:
            bad.append((name, got))
    check(f"照合: 実機の画面 {len(want)} 枚の 状態・アプリ・時間の値を OCR なしで読める", not bad, bad)
    main = lib.read(Image.open(os.path.join(folder, "main.png")))
    check("照合: メイン画面の「設定」ボタンを名前で見つける(OCR では「設」と読み違えていた)", main.find_button("設定", 0.9) is not None)
    sel = lib.read(Image.open(os.path.join(folder, "select_Obsidian.png")))
    check("照合: アプリ選択で選んだ行(青)も名前で見つける", (sel.find_button("Obsidian", 0.9) or UIElement("", "", (0,) * 4)).rect[1] // 2 == 129,
          [b.text for b in sel.buttons])
    kd = Image.open(os.path.join(folder, "keypad_digits.png"))
    check("照合: 数字の字形で 1234567890 を読む", lib.read_number(kd, (18, 132, 250, 32)) == "1234567890",
          lib.read_number(kd, (18, 132, 250, 32)))
    fx = lib.read(Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "screens_extra", "main_focus_settings.png")))
    check("照合: フォーカスが別のボタンにあっても読める(2026-11-10 実機:「アプリケーション」が見つからず中断した)",
          fx.find_button("アプリケーション", 0.8) is not None and am.Operator.current_app(None, fx) == "Mudrock Air"
          and fx.find_button("設定", 0.9) is not None and sm.recognized(fx), [b.text for b in fx.buttons])
    part = ScreenUI(None, [UIElement("button", "OK", (0, 0, 200, 100)), UIElement("button", "", (0, 200, 300, 200))], [], 1.0)
    check("照合: 名前の無い大きなボタンがあれば読めたことにしない", not sm.recognized(part))
    check("照合: 見本に無い画面は読めたことにしない", not sm.recognized(lib.read(Image.new("RGB", (480, 640), "white"))))


def storage_tests():
    """保存の共通処理: 安全な書き込み・設定の読み書き"""
    import storage, tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "new", "sub", "a.csv")
        storage.write_atomic(p, "あ,1\n", encoding="utf-8-sig")
        check("保存: フォルダが無ければ作って書く(文字列・文字コード指定)", open(p, "rb").read() == "あ,1\n".encode("utf-8-sig"))
        storage.write_atomic(p, b"\x00\x01")
        check("保存: バイト列で上書き・一時ファイルを残さない",
              open(p, "rb").read() == b"\x00\x01" and os.listdir(os.path.dirname(p)) == ["a.csv"])
        sp = os.path.join(d, "config", "gui_settings.json")
        check("設定: 無いときは空", storage.load_settings(sp) == {})
        storage.save_settings({"plan": [["Obsidian", [30]]], "名前": "日本語"}, sp)
        check("設定: 保存して読み戻せる", storage.load_settings(sp) == {"plan": [["Obsidian", [30]]], "名前": "日本語"})
        with open(sp, "w", encoding="utf-8") as f:
            f.write("{壊れた")
        check("設定: 壊れていたら空(起動は続ける)", storage.load_settings(sp) == {})
        lp = os.path.join(d, "log", "app_log.txt")
        real_max = storage.APP_LOG_MAX
        storage.APP_LOG_MAX = 400
        try:
            for i in range(40):
                storage.append_log(f"行 {i:02d}", lp)
            body = open(lp, encoding="utf-8").read()
            check("ログのファイル: 大きくなったら古い分を消す(新しい行は残す)", os.path.getsize(lp) <= 600 and "行 39" in body
                  and "行 00" not in body, (os.path.getsize(lp), body[:40]))
        finally:
            storage.APP_LOG_MAX = real_max
        check("PASS: 暗号化の処理は無い(設定にそのまま保存する)", not hasattr(storage, "encrypt_pass"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def exe_tests():
    """exe(PyInstaller)で起動したとき: 基準のフォルダは exe の隣。作成の設定は exe を作らずに確かめる"""
    import storage, tempfile
    d = tempfile.mkdtemp()
    cwd, had, exe0 = os.getcwd(), hasattr(sys, "frozen"), sys.executable
    try:
        sys.frozen, sys.executable = True, os.path.join(d, "Traer5iAuto.exe")
        storage.use_base()
        check("exe: config / TracerData は exe のあるフォルダに置く",
              storage.base_dir() == d and os.path.samefile(os.getcwd(), d), (storage.base_dir(), os.getcwd()))
    finally:
        os.chdir(cwd); sys.executable = exe0
        if not had:
            del sys.frozen
    check("exe: Python で起動したときは VBS のフォルダ", storage.base_dir() == os.path.dirname(ROOT), storage.base_dir())
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_exe
    a = build_exe.pyinstaller_args()
    check("exe 作成: フォルダ形式・窓なし・numpy と OpenCV と OCR(winrt)と Tk は外す・照合の見本と画面(web)を含める・起動は web_app",
          "--onedir" in a and "--onefile" not in a and "--noconsole" in a and "--collect-submodules" not in a
          and {a[i + 1] for i, v in enumerate(a) if v == "--exclude-module"} == {"numpy", "cv2", "winrt", "tkinter"}
          and a[-1].endswith(os.path.join("Traer5iAuto", "src", "web_app.py"))
          and sorted(a[i + 1].rsplit(os.pathsep, 1)[1] for i, v in enumerate(a) if v == "--add-data") == ["screens", "web"], a)
    check("exe 作成: 作業用とできあがりは TRACERexl 直下(配布物の中に作らない)",
          os.path.dirname(build_exe.DIST) == os.path.dirname(os.path.dirname(ROOT)) == os.path.dirname(build_exe.WORK))


def refs_tests():
    """資料一覧: あいまい一致と CSV の読み込み(文字コード・区切りの自動判別)"""
    import refs_core as gr, tempfile, shutil
    check("一覧: 全角/半角・大文字/小文字・ハイフンを無視", gr.norm("Ａ－００１ b_2") == "a001b2", gr.norm("Ａ－００１ b_2"))
    check("一覧: 一致の順位(完全 < 先頭 < 部分 < 順番どおり)",
          [gr.match_rank("a001", v) for v in ("A-001", "A-0012", "XA001", "A-0-0-x-1")] == [0, 1, 2, 3])
    check("一覧: 一致しなければ None・空の入力は全件", gr.match_rank("zz", "A-001") is None and gr.match_rank("", "A-001") == 2)
    d = tempfile.mkdtemp()
    try:
        p1 = os.path.join(d, "sjis.csv")
        open(p1, "wb").write("資料ID,名称\r\nA-001,土器\r\nA-002,石器\r\n".encode("cp932"))
        head, rows, lines = gr.read_csv(p1)
        check("CSV: Shift_JIS を読める", head == ["資料ID", "名称"] and rows == [["A-001", "土器"], ["A-002", "石器"]], (head, rows))
        p2 = os.path.join(d, "tab.csv")
        open(p2, "wb").write("﻿ID\tメモ\nB-1\tx\n\nB-2\n".encode("utf-8"))
        head, rows, lines = gr.read_csv(p2)
        check("CSV: BOM 付き UTF-8・タブ区切り・空行と短い行", head == ["ID", "メモ"] and rows == [["B-1", "x"], ["B-2", ""]], (head, rows))
        check("CSV: 行ごとの元のファイルの行番号(空行も数える)", lines == [2, 4], lines)
        p4 = os.path.join(d, "quoted.csv")
        open(p4, "wb").write('ID,メモ\r\n"A","1行目\r\n2行目"\r\nB,x\r\n'.encode("utf-8"))
        head, rows, lines = gr.read_csv(p4)
        check("CSV: 引用の中の改行があっても行番号は行の始まり", lines == [2, 4] and rows[0][1] == "1行目\r\n2行目", (lines, rows))
        check("並べ替え: 数は数の順・文字は norm の順・空欄は最後",
              sorted(["10", "", "b", "9", "A"], key=gr.sort_key) == ["9", "10", "A", "b", ""])
        p3 = os.path.join(d, "empty.csv"); open(p3, "wb").write(b"\r\n")
        try:
            gr.read_csv(p3); check("CSV: 空はエラー", False)
        except ValueError:
            check("CSV: 空はエラー", True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def checkbox_tests():
    """「デフォルト設定を使用」のチェック欄の位置: 文字の位置がずれても欄の中を押す"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (960, 300), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((141, 111, 170, 140), outline=(0, 0, 0), width=2)      # チェック欄
    ui = ScreenUI(img, [], [], 1.0)
    for x in (190, 171):                                                 # オフ / オン(点線の枠で左へずれる)
        bx, by = am.checkbox_left_of(ui, UIElement("text", "デフォルト設定を使用", (x, 102, 468, 42)))
        check(f"チェック欄: 文字の左端 {x} でも欄の中を押す", 141 < bx < 170 and 111 < by < 140, (bx, by))
    bx, _ = am.checkbox_left_of(ScreenUI(Image.new("RGB", (960, 300), (255, 255, 255)), [], [], 1.0),
                                UIElement("text", "デフォルト設定を使用", (190, 102, 468, 42)))
    check("チェック欄: 見つからなければ決め打ちの位置", bx == 190 - am.DEFAULT_BOX_DX, bx)


def path_tests():
    """config / TracerData は VBS のフォルダ基準の相対パス"""
    import storage as paths, tempfile
    check("パス: 設定・記録・異常時の画面・保存先は相対パス",
          not any(os.path.isabs(p) for p in (paths.SETTINGS, paths.LOG_CSV, paths.ERROR_PNG, paths.TRACER_DIR)))
    check("パス: 異常時の画面と設定は config の中",
          all(os.path.normpath(p).split(os.sep)[0] == "config" for p in (paths.ERROR_PNG, paths.SETTINGS)))
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.gettempdir())
        paths.use_base()
        check("パス: VBS のフォルダ以外から起動したら、VBS のフォルダへ移る",
              os.path.exists(paths.VBS) and os.getcwd() == os.path.dirname(ROOT), os.getcwd())
    finally:
        os.chdir(cwd)


REAL_TO_MAIN = am.to_main          # main() で差し替える前の本物(自動で戻す処理の確認用)


class ScriptOp:
    """画面の移り変わりを表で決める偽の Operator(to_main / click_until の確認用)。
    moves: {(画面, 押すボタン): 次の画面}。押しても進まない回数を misses で指定できる"""

    def __init__(self, screen, moves, misses=0, unknown_reads=0):
        self.screen, self.moves, self.misses, self.unknown_reads = screen, moves, misses, unknown_reads
        self.clicks, self.ui = [], ScreenUI(None, [], [], 1.0)

    def state(self, ui=None):
        if self.unknown_reads:
            self.unknown_reads -= 1
            return "unknown"
        return self.screen

    def read(self):
        return self.ui

    def click(self, label, **kw):
        self.clicks.append(label)
        if self.misses:
            self.misses -= 1
            return
        self.screen = self.moves.get((self.screen, label), self.screen)

    def click_elem(self, ui, e, wait=0.25):
        self.click(e.text)

    def wait_state(self, want, timeout, interval=0.6):
        want = (want,) if isinstance(want, str) else want
        if self.screen not in want:
            raise am.DeviceError(f"画面 {want} を待ちましたが、現在は '{self.screen}' です")
        return self.ui


def safety_tests():
    """どんな中断でもトリガーを離す・手で引かれていたら照射しない・読み違い1回は続ける・自動で戻す"""
    op = FakeOp(None, [])
    am.progress = lambda e, t: None
    try:
        am.measure(op, 30)
        check("超過: 終わらなければ中断", False)
    except am.Overrun as e:
        check("超過: 表の時間+600秒で「計測超過」として中断", "600" in str(e), e)
    check("超過: 中断したらトリガーを離す(照射を止める)", op.trig == "pull", op.trig)

    class NoStart(FakeOp):            # トリガー後に測定中の画面にならない
        def wait_state(self, want, timeout, interval=0.6):
            raise am.DeviceError("測定中になりません")
    op = NoStart(30, [])
    try:
        am.measure(op, 30)
    except am.DeviceError:
        pass
    check("中断: 測定中にならなくてもトリガーを離す", op.trig == "pull", op.trig)

    op = FakeOp(30, ["時間 30.0"]); op.trig = "release"
    try:
        am.measure(op, 30)
        check("手動: 前から引かれていたら照射しない", False)
    except am.ManualTrigger:
        check("手動: 前から引かれていたら照射せずに中断(こちらからは押さない)", op.t_trigger is None and op.trig == "release")

    class OddOnce(FakeOp):           # 測定中に1回だけ読み違える
        n = 0
        def _screen(self):
            st, ui = super()._screen()
            if st == "measuring":
                self.n += 1
                if self.n == 3:
                    return "unknown", ui
            return st, ui
    _, info, _ = (lambda o: (o, am.measure(o, 30), None))(OddOnce(30, ["時間 30.0"]))
    check("読み違い: 1回だけなら測定を続ける", info.get("inst_time") == "30.0", info)

    class OddTwice(OddOnce):
        def _screen(self):
            st, ui = FakeOp._screen(self)
            return ("unknown", ui) if st == "measuring" and Clock.t - self.t_trigger > 6 else (st, ui)
    op = OddTwice(30, [])
    try:
        am.measure(op, 30)
        check("想定外の画面: 2回続いたら機器エラー", False)
    except am.DeviceError as e:
        check("想定外の画面: 測定中に2回続いたら機器エラー(画面認識エラーではない)で中断し、トリガーを離す",
              op.trig == "pull" and e.kind == "device", (op.trig, e.kind))

    try:
        am.parse_plan("Obsidian=62.5")
        check("整数: コマンドの小数は受け付けない", False)
    except ValueError:
        check("整数: コマンドの小数は受け付けない(GUI は入力欄で整数のみ)", am.parse_plan("A=30,B=15+60") == [("A", [30.0]), ("B", [15.0, 60.0])])

    # 自動でメイン画面に戻す
    for start, moves, want_clicks in (
            ("settings", {("settings", "キャンセル"): "main"}, ["キャンセル"]),
            ("select_app", {("select_app", "キャンセル"): "main"}, ["キャンセル"]),
            ("confirm", {("confirm", "OK"): "main"}, ["OK"])):
        op = ScriptOp(start, moves)
        REAL_TO_MAIN(op)
        check(f"自動で戻す: {start} → メイン画面", op.screen == "main" and op.clicks == want_clicks, (op.screen, op.clicks))
    op = ScriptOp("main", {}, unknown_reads=2)
    REAL_TO_MAIN(op)
    check("自動で戻す: 読めない画面は読み直す", op.screen == "main")
    op = ScriptOp("keypad", {})
    try:
        REAL_TO_MAIN(op); check("自動で戻す: 数値入力画面は戻さない", False)
    except am.DeviceError:
        check("自動で戻す: 数値入力画面は自動で戻さず 機器エラー", op.clicks == [])

    # 押しそこねたら1回だけ押し直す
    op = ScriptOp("main", {("main", "アプリケーション"): "select_app"}, misses=1)
    am.Operator.click_until(op, "アプリケーション", "select_app", 10)
    check("押し直し: 画面が進まなければ1回だけ押し直す", op.screen == "select_app" and op.clicks == ["アプリケーション"] * 2, op.clicks)
    op = ScriptOp("main", {}, misses=5)
    try:
        am.Operator.click_until(op, "アプリケーション", "select_app", 10); check("押し直し: 2回で諦める", False)
    except am.DeviceError:
        check("押し直し: 押し直しても進まなければ 機器エラー", op.clicks == ["アプリケーション"] * 2, op.clicks)


def settle_tests():
    """画面の遷移の遅れへの備え: 描きかけの画面は読み直す / ボタンが出るまで数秒待つ(機器の実際の画面の見本で確かめる)"""
    from PIL import Image
    import screen_match
    lib = screen_match.library()
    scr = lambda f: lib.read(Image.open(os.path.join(ROOT, "screens", f)).convert("RGB"))
    new = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "login_partial.png")   # 実機で残った描きかけのログイン画面
    partial = lib.read(Image.open(new).convert("RGB"))

    class Rc:
        def __init__(self, seq):
            self.seq, self.n = list(seq), 0
        def capture_background(self):
            return Image.new("RGB", (1, 1))
        def read_ui(self, img=None):
            ui = self.seq[min(self.n, len(self.seq) - 1)]
            self.n += 1
            return ui

    def op_with(seq):
        op = am.Operator.__new__(am.Operator)
        op.rc, op.ui = Rc(seq), None
        return op
    saved = am.SETTLE_RETRY
    try:
        if partial is not None:
            op = op_with([partial, partial, scr("login.png")])
            ui = op.read()
            check("遷移: 描きかけの画面(見本で読めない)は少し待って読み直す", screen_match.recognized(ui) and op.rc.n == 3, op.rc.n)
        op = op_with([scr("main.png")])
        op.read()
        check("遷移: 読める画面は1回だけ読む", op.rc.n == 1, op.rc.n)
        am.SETTLE_RETRY = 0
        login = scr("login.png")
        op = op_with([login] * 4 + [scr("main.png")])     # ボタンが出るまで約2秒かかる
        ui, e = op.find("アプリケーション", min_score=0.8)
        check("遷移: ボタンがすぐ出なくても、FIND_WAIT_S 秒まで読み直して待つ", e is not None and op.rc.n == 5, op.rc.n)
        op = op_with([login])
        try:
            op.find("アプリケーション", min_score=0.8)
            check("遷移: 待っても出なければ画面認識エラー", False)
        except am.OcrError:
            check("遷移: FIND_WAIT_S 秒待っても出なければ画面認識エラー", op.rc.n >= am.FIND_WAIT_S / 0.5, op.rc.n)
        # 起動画面(電源を入れた直後): 見本で読め、状態は startup。メイン画面へ戻す処理は「ログイン」を押してログイン画面へ
        su = scr("startup.png")
        op0 = am.Operator.__new__(am.Operator)
        check("起動画面: 見本で読め、状態は startup(「ログイン」のボタン)", screen_match.recognized(su)
              and am.Operator.state(op0, su) == "startup" and su.find_button("ログイン", 0.8) is not None)
        am.SETTLE_RETRY = 3
        op = op_with([su])
        clicks = []
        login_ui = scr("login.png")
        op.click_elem = lambda ui, e, wait=0.25: (clicks.append(e.text), setattr(op, "ui", None),
                                                  setattr(op.rc, "seq", [login_ui]))   # 押すとログイン画面になる
        real_login, real_pass = am.login, am.PASSCODE
        logins = []
        am.login = lambda o, code: (logins.append(code), (_ for _ in ()).throw(am.LoginWait("PASS なし")))
        try:
            am.PASSCODE = ""
            try:
                REAL_TO_MAIN(op)
                check("起動画面: 「ログイン」を押してログイン画面へ", False)
            except am.LoginWait:
                check("起動画面: メイン画面へ戻す処理は「ログイン」を押してログイン画面へ(PASS が無ければログイン待ち)",
                      clicks == ["ログイン"] and logins == [""], (clicks, logins))
        finally:
            am.login, am.PASSCODE = real_login, real_pass
        # ログイン: 電源を入れた後の最初は、ログインの後に放射線の注意の画面になる。これも成功(ログイン失敗にしない)
        for after, name in (("safety.png", "放射線の注意"), ("confirm.png", "現在の構成"), ("main.png", "メイン")):
            op = op_with([scr("login.png")])
            done = scr(after)
            pressed = []

            def press(ui, e, wait=0.25, op=op, done=done, pressed=pressed):
                pressed.append(e.text)
                op.ui = None
                if e.text == "OK":
                    op.rc.seq = [done]                  # OK を押すと次の画面になる
            op.click_elem = press
            try:
                real_login(op, "1234")
                check(f"ログイン: 後の画面が{name}でも成功(ログイン失敗にしない)", pressed[-1] == "OK", pressed)
            except am.LoginFailed:
                check(f"ログイン: 後の画面が{name}でも成功(ログイン失敗にしない)", False, pressed)
        import device_link as dl_mod
        # 起動の途中(初期化中)・放射線の注意(電源を入れた後の最初のログインのあと)
        check("起動の途中: 見本で読め、状態は booting", am.Operator.state(op0, scr("booting.png")) == "booting")
        check("放射線の注意: 見本で読め、状態は safety", am.Operator.state(op0, scr("safety.png")) == "safety"
              and screen_match.recognized(scr("safety.png")))
        for f, want in (("booting.png", "起動の途中"), ("safety.png", "放射線の注意")):
            op = op_with([scr(f)])
            try:
                REAL_TO_MAIN(op)
                check(f"{want}: 測定の処理からは進めない", False)
            except am.DeviceError:
                check(f"{want}: メイン画面へ戻す処理(測定の処理)からは進めず、機器エラー", True)
        check("確認のトリガー: アプリから送る機能は無い(機器はアプリからの命令に反応しなかった。本体のトリガーを手で操作する)",
              not hasattr(am, "ack_safety") and not hasattr(dl_mod, "ACK_HOLD_S")
              and not hasattr(dl_mod.DeviceLink, "acknowledge"))
        # 機器へつなぐ: 失敗してもすぐにはつなぎ直さない(機器が前の接続の後始末を終えるまで答えないため。試し直しは待ち受けが間を空けて行う)
        import device_link as dl
        calls = []

        def flaky(self):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("timed out")
            self._socks = {"screen": None}
        saved_dl = dl.DeviceLink.connect, dl.DeviceLink.close, dl.LINK, dl.device_present
        dl.DeviceLink.connect, dl.DeviceLink.close, dl.LINK = flaky, lambda self, abort=False: None, None
        dl.device_present = lambda *a, **k: True        # 本物の機器を探さない
        try:
            try:
                dl.shared_link()
                check("接続: 失敗したらすぐにはつなぎ直さない", False)
            except OSError:
                check("接続: 失敗したら、その場ではつなぎ直さず失敗を返す(1回だけ試す)", len(calls) == 1, calls)
            link = dl.shared_link()
            check("接続: 次に呼ばれたとき(待ち受けの次の回)につながる", len(calls) == 2 and link.connected, calls)
            # 拒否(初期化の途中で、まだ受け付けていない)は、準備ができるまで試し直す
            dl.LINK = None; refused = []

            def refuse_twice(self):
                refused.append(1)
                if len(refused) <= 2:
                    raise ConnectionRefusedError("拒否")
                self._socks = {"screen": None}
            dl.DeviceLink.connect = refuse_twice
            saved_rr = dl.REFUSED_RETRY_S
            dl.REFUSED_RETRY_S = 0.01
            try:
                link = dl.shared_link()
                check("接続: 拒否(初期化の途中)は、準備ができるまで試し直してつながる", len(refused) == 3 and link.connected, refused)
            finally:
                dl.REFUSED_RETRY_S = saved_rr
                dl.DeviceLink.connect = flaky
            check("接続: 情報を待つのは長く(起動の途中は届くまで時間がかかる。切らずに待つ)",
                  dl.INFO_WAIT_S >= 30 and dl.CONNECT_TIMEOUT_S >= 5, (dl.INFO_WAIT_S, dl.CONNECT_TIMEOUT_S))
            # GUI(AUTO_CONNECT=False): 状態の読み取り・測定はつなぎに行かない。つなぐのは待ち受け・「再接続」(connect=True)だけ
            saved_auto = dl.AUTO_CONNECT
            try:
                dl.AUTO_CONNECT = False
                dl.LINK = None; calls.clear()
                try:
                    dl.shared_link()
                    check("接続の受け持ち: つながっていなければ、状態の読み取りはつなぎに行かない", False)
                except OSError as e:
                    check("接続の受け持ち: GUI では、つながっていなければ状態の読み取り・測定はつなぎに行かず「つながっていません」",
                          not calls and "つながっていません" in str(e), (calls, str(e)))
                calls[:] = [1]                          # 次の connect は成功する
                link = dl.shared_link(connect=True)
                check("接続の受け持ち: 待ち受け・「再接続」(connect=True)はつなぐ", calls == [1, 1] and link.connected, calls)
                check("接続の受け持ち: つながっていれば、状態の読み取りはその接続を使う", dl.shared_link() is link and calls == [1, 1])
            finally:
                dl.AUTO_CONNECT = saved_auto
            # 機器がいなければ(USB が無い・電源が切れている)、つなぎに行かずにすぐ「見つかりません」
            dl.LINK = None; calls.clear()
            dl.device_present = lambda *a, **k: False
            t0 = _time.time()
            try:
                dl.shared_link()
                check("接続: 機器がいなければすぐ失敗", False)
            except ConnectionError as e:
                check("接続: 機器がいなければ(USB・電源)、つなぎに行かず(10 秒×2 待たず)すぐ「見つかりません」",
                      not calls and "見つかりません" in str(e) and _time.time() - t0 < 1, (calls, str(e)))
        finally:
            dl.DeviceLink.connect, dl.DeviceLink.close, dl.LINK, dl.device_present = saved_dl
    finally:
        am.SETTLE_RETRY = saved


def retry_tests():
    """途中で止まった回(失敗)の判定と、確認してからの測り直し(案 A)"""
    row = lambda inst: {"set_s": 30.0, "inst_time": inst}
    check("失敗: 照射の途中で切れて、機器の時間が設定より短い・読めない回は失敗(接続エラー。台帳の理由に秒数は出さない)",
          am.failure(row("8.0"), set_each=False, cause="cut") == "接続エラー"
          and am.failure(row(""), set_each=False, cause="cut") == "接続エラー")
    check("失敗: 時間を毎回書き込むとき、機器の時間が設定より短ければ失敗(停止エラー)",
          am.failure(row("26.0"), set_each=True) == "停止エラー")
    empty = {"set_s": 30.0, "inst_time": "1.0", "empty": True}
    check("失敗: 結果画面の元素の表が空なら、時間を書き込まないときでも機器エラー(接続切れなら接続エラーが先)",
          am.failure(empty, set_each=False) == "機器エラー" and am.failure(empty, set_each=True) == "機器エラー"
          and am.failure(empty, set_each=False, cause="cut") == "接続エラー")
    import screen_match
    from PIL import Image
    here = os.path.dirname(os.path.abspath(__file__))
    low = screen_match.library().read(Image.open(os.path.join(here, "fixtures", "result_low_count.png")).convert("RGB"))
    normal = [screen_match.library().read(Image.open(os.path.join(os.path.dirname(am.__file__), "screens", f"result_{m}.png")).convert("RGB"))
              for m in ("Mudrock_Air", "Obsidian")]
    check("結果画面: 照射対象なし(「計数率が低すぎます。」・表が空)は表が空と分かる。ふつうの結果画面は空でない",
          am.screen_state(low) == "result" and am.result_empty(low) and am.result_info(low).get("empty")
          and am.result_info(low).get("sample_no") == "784" and not any(am.result_empty(u) for u in normal)
          and not any(am.result_info(u).get("odd") for u in normal + [low]))
    frame = lambda name: Image.open(os.path.join(os.path.dirname(am.__file__), "screens", name + ".png")).convert("RGB")
    samples = screen_match.title_samples()
    check("題名: 結果画面・測定中の画面の題名から、どのモードか分かる。照射対象なし(知らせの文)はどのモードでもない",
          screen_match.match_title(frame("result_Mudrock_Air"), samples) == "Mudrock Air"
          and set(samples) >= {"Mudrock Air", "Obsidian", "Glass Dual", "Spectrometer Mode"}
          and screen_match.match_title(Image.open(os.path.join(here, "fixtures", "result_low_count.png")).convert("RGB"), samples) == ""
          and am.result_info(low).get("title") == "" and am.result_info(normal[0]).get("title") == "Mudrock Air")
    gd = screen_match.library().read(Image.open(os.path.join(here, "fixtures", "result_glass_dual.png")).convert("RGB"))
    sp = screen_match.library().read(Image.open(os.path.join(here, "fixtures", "result_spectrometer.png")).convert("RGB"))
    check("結果画面: Glass Dual・Spectrometer Mode(平均化が無く題名は「スペクトル」)の結果画面も結果と分かり、題名・No・時間を読める",
          am.screen_state(gd) == "result" and am.result_info(gd).get("title") == "Glass Dual" and am.result_info(gd).get("sample_no") == "785"
          and am.screen_state(sp) == "result" and am.result_info(sp).get("title") == "Spectrometer Mode"
          and am.result_info(sp).get("sample_no") == "787" and am.result_info(sp).get("inst_time") == "10.0"
          and not am.result_info(sp).get("empty"), (am.screen_state(sp), am.result_info(sp)))
    ino = screen_match.library().read(Image.open(os.path.join(here, "fixtures", "main_inoperable.png")).convert("RGB"))
    check("画面の判定: 「作動できません」のメイン画面(試験準備完了の文字が無い・情報の編集がある)を結果画面にしない",
          am.screen_state(ino) != "result", am.screen_state(ino))
    lib_states = {f[:-4]: am.screen_state(screen_match.library().read(frame(f[:-4])))
                  for f in os.listdir(os.path.join(os.path.dirname(am.__file__), "screens")) if f.endswith(".png") and f != "keypad_digits.png"}
    check("画面の判定: 見本の画面の判定は変わらない(メイン・測定中は結果にならない)",
          all(v == "main" for k, v in lib_states.items() if k.startswith("main"))
          and all(v == "measuring" for k, v in lib_states.items() if k.startswith("measuring"))
          and all(v == "result" for k, v in lib_states.items() if k.startswith("result")), lib_states)
    tr = lambda title, known=True: {"set_s": 30.0, "inst_time": "30.0", "app": "Mudrock Air", "title": title, "title_known": known}
    check("失敗: 題名がモードの名前でない(知らせの文・別のモード)なら機器エラー。そのモードの見本が無ければ題名では判定しない",
          am.failure(tr(""), False) == "機器エラー" and am.failure(tr("Obsidian"), False) == "機器エラー"
          and am.failure(tr("Mudrock Air"), False) == "" and am.failure(tr("", known=False), False) == "")
    check("失敗: 結果画面だが見本で読めない部分がある(odd)なら機器エラー",
          am.failure({"set_s": 30.0, "inst_time": "30.0", "odd": True}, set_each=False) == "機器エラー")
    check("失敗: 時間を書き込まないときの短さ(機器の設定が違うだけ)・時間どおりの回は失敗にしない",
          am.failure(row("26.0"), set_each=False) == "" and am.failure(row("30.0"), set_each=True, cause="cut") == "")
    real_step, real_main = am._run_step, am.to_main
    results = iter([{"app": "A", "sample_no": "10", "failed": "接続切れ 8/30 秒"},
                    {"app": "A", "sample_no": "11"}, {"app": "B", "sample_no": "12", "failed": "時間不足 5/30 秒"}])
    calls = []
    am._run_step = lambda op, i, n, app, phases, set_each, *a: (calls.append((i, app)), dict(next(results)))[1]
    am.to_main = lambda op: None
    try:
        am.run_plan(object(), [("A", [30.0]), ("B", [30.0])])
        check("失敗の回: その場では測り直さず次の回へ(測り直しはターンの後の「再計測」。2026-11-16 ユーザーの判断)",
              calls == [(1, "A"), (2, "B")], calls)
        check("測り直しの仕組み(confirm_retry・RETRY_MAX)は無い", not hasattr(am, "RETRY_MAX")
              and "confirm_retry" not in am.run_plan.__code__.co_varnames)
    finally:
        am._run_step, am.to_main = real_step, real_main
    # 照射を始めた後に停止: 途中までの測定を失敗(停止)として知らせてから停止する。照射の前の停止は台帳に入れない
    real = (am.measure, am.stopped_result, am.switch_app, am.set_phase_times, am.log)
    am.switch_app = am.set_phase_times = lambda *a: None
    am.log = lambda row: None
    try:
        for started in (True, False):
            got = []

            def stop(op, sec, started=started):
                if started:
                    am.STEP["t0"] = am.time.time()
                raise am.Cancelled("ユーザーが停止しました")
            am.measure = stop
            am.stopped_result = lambda op, wait=0: {"sample_no": "777", "inst_time": "12.0"}
            try:
                am._run_step(object(), 1, 1, "Obsidian", [30.0], True, lambda *a: None, got.append,
                             lambda s: None, lambda k: None, None, None, False)
                raised = False
            except am.Cancelled:
                raised = True
            if started:
                check("停止: 照射を始めた後の停止は、途中までの回(No と機器の時間)を失敗「停止エラー」(時間は出さない)として知らせてから止まる",
                      raised and len(got) == 1 and got[0]["failed"] == "停止エラー" and got[0]["sample_no"] == "777"
                      and got[0]["status"] == "failed", got)
            else:
                check("停止: 照射の前の停止は台帳に入れない(知らせない)", raised and not got, got)
        am.measure = lambda op, sec: (am.STEP.__setitem__("t0", am.time.time()), (_ for _ in ()).throw(am.Cancelled("x")))[1]
        am.stopped_result = lambda op, wait=0: {}
        got = []
        try:
            am._run_step(object(), 1, 1, "Obsidian", [30.0], True, lambda *a: None, got.append,
                         lambda s: None, lambda k: None, None, None, False)
        except am.Cancelled:
            pass
        check("停止: 結果画面が読めなくても、失敗「停止エラー」(No なし)として知らせる", len(got) == 1 and got[0]["failed"] == "停止エラー", got)
        for set_each in (True, False):          # 照射が終わる瞬間に押した: 結果画面の時間が設定どおりなら有効(停止エラーにしない)
            am.stopped_result = lambda op, wait=0: {"sample_no": "778", "inst_time": "30.0"}
            got = []
            try:
                am._run_step(object(), 1, 1, "Obsidian", [30.0], set_each, lambda *a: None, got.append,
                             lambda s: None, lambda k: None, None, None, False)
                raised = False
            except am.Cancelled:
                raised = True
            check(f"停止: 照射の終わる瞬間に押して時間が設定どおりなら有効(フェーズ設定{'あり' if set_each else 'スキップ'})・ターンは止まる",
                  raised and len(got) == 1 and not got[0].get("failed") and got[0]["status"] == "ok"
                  and got[0]["sample_no"] == "778", got)
            am.stopped_result = lambda op, wait=0: {"sample_no": "779", "inst_time": "12.0"}
            got = []
            try:
                am._run_step(object(), 1, 1, "Obsidian", [30.0], set_each, lambda *a: None, got.append,
                             lambda s: None, lambda k: None, None, None, False)
            except am.Cancelled:
                pass
            check(f"停止: 時間が短ければ停止エラー(フェーズ設定{'あり' if set_each else 'スキップ'}でも。ずれのアラートは出さない)",
                  len(got) == 1 and got[0].get("failed") == "停止エラー" and "mismatch" not in got[0], got)
        check("失敗: 停止して時間が設定どおりでも、表が空なら機器エラー",
              am.failure({"set_s": 30.0, "inst_time": "30.0", "empty": True}, True, "stop") == "機器エラー")
        check("失敗: 照射の後に中断した回は、結果画面の時間が設定どおりでも機器エラー",
              am.failure({"set_s": 30.0, "inst_time": "30.0"}, True, "device") == "機器エラー")
        # 照射を始めた後の中断も台帳に残す: 思わぬ画面などは機器エラー(途中の結果を読む)、つなぎ直せない接続切れは接続エラー
        for exc, want, reads in ((am.DeviceError("測定中に想定外の画面"), "機器エラー", True),
                                 (am.Overrun("時間超過"), "機器エラー", True), (ConnectionError("切れた"), "接続エラー", False)):
            read_calls, got = [], []
            am.measure = lambda op, sec, exc=exc: (am.STEP.__setitem__("t0", am.time.time()), (_ for _ in ()).throw(exc))[1]
            am.stopped_result = lambda op, wait=0: (read_calls.append(1), {"sample_no": "790"})[1]
            real_wait, am.wait_reconnect = am.wait_reconnect, lambda op, on_wait, *a: (_ for _ in ()).throw(ConnectionError("つながらない"))
            try:
                am._run_step(object(), 1, 1, "Obsidian", [30.0], True, lambda *a: None, got.append,
                             lambda s: None, lambda k: None, None, None, False)
            except Exception:
                pass
            finally:
                am.wait_reconnect = real_wait
            check(f"照射の後の中断: {type(exc).__name__} も台帳に失敗「{want}」として残す"
                  + ("(途中の結果画面から No を読む)" if reads else "(画面は読まない)"),
                  len(got) == 1 and got[0]["failed"] == want and bool(read_calls) == reads
                  and (got[0].get("sample_no") == "790") == reads, got)
        am.measure = lambda op, sec: (_ for _ in ()).throw(am.DeviceError("照射の前"))
        got = []
        try:
            am._run_step(object(), 1, 1, "Obsidian", [30.0], True, lambda *a: None, got.append,
                         lambda s: None, lambda k: None, None, None, False)
        except am.DeviceError:
            pass
        check("照射の前の中断(トリガーを引く前)は台帳に入れない", not got, got)
        check("照射の後の中断も判定は 1 つ(_record_failed は無い)", not hasattr(am, "_record_failed"))
    finally:
        am.measure, am.stopped_result, am.switch_app, am.set_phase_times, am.log = real

def lid_tests():
    """蓋の磁気センサー(LID_SENSOR.md): 蓋が閉じていなければ照射しない・照射中に開いたらトリガーを離して「機器エラー」"""
    real = (am._sleep, am.switch_app, am.set_phase_times, am.stopped_result, am.log, am.LID_OK)

    def vsleep(t):                      # 仮想の時計を進め、停止の要求(蓋が開いた)に応える
        Clock.t += max(0.0, t)
        if am.CANCEL.is_set():
            raise am.Cancelled("停止")
    am._sleep, am.log = vsleep, lambda row: None
    am.set_phase_times = lambda *a: None
    am.stopped_result = lambda op, wait=0: {"sample_no": "880", "inst_time": "9.0"}
    lid = {"closed": True}
    am.LID_OK = lambda: lid["closed"]
    try:
        row = lambda inst: {"set_s": 30.0, "inst_time": inst}
        check("蓋: 照射中に開いて機器の時間が短い・読めない回は「機器エラー」",
              am.failure(row("9.0"), True, "lid") == am.FAIL_DEVICE == "機器エラー" and am.failure(row(""), False, "lid") == "機器エラー")
        check("蓋: 照射の終わる瞬間に開いて時間が設定どおりなら有効", am.failure(row("30.0"), True, "lid") == "")
        # 開いているときは照射しない(測定の直前に確かめる)
        lid["closed"] = False
        op = FakeOp(30, ["88000-01", "時間 30.0"])
        try:
            am.measure(op, 30)
            raised = False
        except am.LidOpen:
            raised = True
        check("蓋: 開いていれば measure はトリガーを引かない(LidOpen)", raised and op.t_trigger is None and op.trig == "pull")
        am.switch_app = lambda op, app: None
        try:
            am.run_plan(op, [("Obsidian", [30.0])])
            raised = False
        except am.LidOpen:
            raised = True
        check("蓋: 開いていれば run_plan は始めない(LidOpen)", raised and op.t_trigger is None)
        lid["closed"] = True
        am.CANCEL.clear()
        am.lid_opened()
        check("蓋: 測定していないときに開いても停止の要求は立てない", not am.CANCEL.is_set() and not am.LID_TRIPPED.is_set())
        # 照射中に開いた: トリガーを離し、その回は機器エラー、LidOpen で止まる(次の回へ進まない)
        op = FakeOp(None, [])
        real_state = op.state

        def state(ui=None):
            st = real_state(ui)
            if len(op.polls) == 3:          # 照射の途中で蓋が開く
                lid["closed"] = False
                am.lid_opened()
            return st
        op.state = state
        got, steps, traces = [], [], []
        real_trace, am.TRACE = am.TRACE, traces.append
        try:
            am.run_plan(op, [("Obsidian", [30.0]), ("Mudrock Air", [30.0])], on_result=got.append,
                        on_step=lambda i, n, a, sec: steps.append(a))
            raised = None
        except Exception as e:
            raised = e
        check("蓋: 照射中に開いたらトリガーを離す", op.t_trigger is not None and op.trig == "pull", op.trig)
        check("蓋: 照射中に開いた回は失敗「機器エラー」として台帳に知らせる",
              len(got) == 1 and got[0].get("failed") == "機器エラー" and got[0].get("sample_no") == "880", got)
        am.TRACE = real_trace
        check("蓋: 照射中に開いたら LidOpen で止まり、次の回へ進まない", isinstance(raised, am.LidOpen) and steps == ["Obsidian"],
              (raised, steps))
        # 切替の途中で開いた: 照射せずに LidOpen
        lid["closed"] = True
        am.CANCEL.clear()
        op = FakeOp(30, ["88100-01", "時間 30.0"])

        def switch(op_, app):
            lid["closed"] = False
            am.lid_opened()
            am._sleep(0.5)
        am.switch_app = switch
        got = []
        try:
            am.run_plan(op, [("Obsidian", [30.0])], on_result=got.append)
            raised = None
        except Exception as e:
            raised = e
        check("蓋: 切替の途中で開いたら照射せずに LidOpen(台帳に入れない)",
              isinstance(raised, am.LidOpen) and op.t_trigger is None and not got, (raised, got))
        check("蓋: 終わったら測定の途中の印を外す", not am.RUNNING.is_set())
    finally:
        am.CANCEL.clear()
        am.LID_TRIPPED.clear()
        am._sleep, am.switch_app, am.set_phase_times, am.stopped_result, am.log, am.LID_OK = real

def trigger_log_tests():
    """トリガーの状態(55101)が変わったときだけ、ミリ秒の時刻つきでログのファイルに残す(繰り返し届く NOT_READY は 1 回)"""
    import device_link as dl
    notes, real = [], dl.NOTE
    dl.NOTE = notes.append
    try:
        link = dl.DeviceLink("127.0.0.1")
        link._lines = lambda kind: iter(["TRIGGER_NOT_READY", "TRIGGER_NOT_READY", "TRIGGER_RELEASED", "TRIGGER_PULLED"])
        link._status_loop()
    finally:
        dl.NOTE = real
    import re
    check("トリガー: 状態が変わったときだけログに残す(ミリ秒の時刻つき)",
          [n.split("(")[0] for n in notes] == ["[トリガー] None → not_ready", "[トリガー] not_ready → released",
                                               "[トリガー] released → pulled"]
          and all(re.search(r"\(\d\d:\d\d:\d\d\.\d{3}\)$", n) for n in notes), notes)

def release_tests():
    """止める命令: 蓋の糸からすぐ送る・2 つの糸から二重に送らない・照射を始める方へは決して切り替えない・遅れの内訳をログに"""
    import threading as _th
    import device_link as dl
    # DeviceLink.release: 照射中だけ・送り直しは RELEASE_AGAIN_S の後
    link = dl.DeviceLink("127.0.0.1")
    sent = []
    link.command = lambda text, timeout=5.0: (sent.append(text), True)[1]
    link.wait_status = lambda want, timeout: True
    link.status = "released"
    link.release()
    check("止める: 照射中でなければ命令を送らない", sent == [], sent)
    link.status = "pulled"
    link.release(); link.release()
    check("止める: 2 回呼んでも(蓋の糸と測定の糸)命令は 1 回だけ・OK の時刻を持つ",
          sent == ["TRIGGER_RELEASE"] and link.release_ok >= link.release_sent > 0, (sent, link.release_sent, link.release_ok))
    link.release_sent -= dl.RELEASE_AGAIN_S + 1
    link.release()
    check("止める: 時間がたっても照射中のままなら送り直す", sent == ["TRIGGER_RELEASE"] * 2, sent)
    # DirectRemote.release は照射を始める方へ切り替えない
    pulls = []
    link2 = dl.DeviceLink("127.0.0.1")
    link2.pull = lambda: pulls.append(1)
    link2.status = "released"
    dl.DirectRemote(link2).release()
    check("止める: 機器へ直接のときは、照射していなければ何もしない(TRIGGER_PULL を送らない)", not pulls)

    class RC:
        def __init__(self): self.calls = []
        def release(self): self.calls.append("release")
        def trigger(self): self.calls.append("trigger")
    op = type("Op", (), {})()
    op.rc = RC()
    op.trigger_state = lambda: "pull"
    op.trigger = lambda: op.rc.calls.append("toggle")
    am.stop_irradiation(op)
    check("止める: stop_irradiation は止める命令だけ(切り替えは使わない)", op.rc.calls == ["release"], op.rc.calls)
    # 蓋が開いたら、センサーの糸からすぐ止め、内訳をログに残す
    traces, real = [], am.TRACE
    am.TRACE = traces.append
    link3 = dl.DeviceLink("127.0.0.1")
    link3.status = "pulled"

    def fake_release():
        link3.release_sent = am.time.time() + 0.010
        link3.release_ok = link3.release_sent + 0.6
        link3.status, link3.status_at = "not_ready", link3.release_sent + 1.0
        return True
    link3.release = fake_release
    op3 = type("Op", (), {})()
    op3.rc = type("RC", (), {"link": link3})()
    am.CANCEL.clear(); am.LID_TRIPPED.clear()
    am._OP = op3
    am.RUNNING.set()
    try:
        before = set(_th.enumerate())
        am.lid_opened()
        for t in set(_th.enumerate()) - before:
            t.join(2)
    finally:
        am.RUNNING.clear(); am._OP = None
        am.CANCEL.clear(); am.LID_TRIPPED.clear()
        am.TRACE = real
    check("蓋: 開いたらセンサーの糸からすぐ止める命令を送り、内訳(命令まで・OK まで・離れた知らせまで)をログに残す",
          link3.status == "not_ready" and len(traces) == 1 and "止める命令まで" in traces[0] and "OK まで" in traces[0]
          and "離れた知らせ(55101)まで" in traces[0], traces)


def main() -> int:
    saved = (am.time, am._sleep, am.to_main, am.say, am.progress, am.log, am.switch_app, am.set_phase_times)
    am.time = Clock
    am._sleep = lambda t: setattr(Clock, "t", Clock.t + max(0.0, t))
    am.to_main = lambda op: None
    am.say = lambda m: None
    try:
        # ---- 終了は画面で判定する
        op, info, _ = run_measure(30, 26, ["70300-01", "時間 26.0"])
        check("終了: 機器が表より早く終われば、表の秒数を待たずに終わる", info["elapsed"] < 30, info)
        check("終了: 早く終わっても検出の遅れは確認間隔(3秒)以内", info["elapsed"] - 26 <= 3, info)
        check("時間: 結果画面の「時間」を読む(早い場合)", info.get("inst_time") == "26.0", info)
        check("No: 結果画面からファイル No を読む", info.get("sample_no") == "703", info)

        op, info, prog = run_measure(30, 80, ["70400-01", "時間 80.0"])
        check("終了: 機器が表より長ければ、結果画面が出るまで待つ", 80 <= info["elapsed"] <= 81.5, info)
        check("終了: 表の秒数を過ぎても中断しない(終了待ちを進捗に出す)", max(prog) > 30, max(prog))
        check("確認間隔: 表の時間までは3秒以下、過ぎたら1秒ごと",
              all(b - a <= 3.01 for a, b in zip(op.polls, op.polls[1:]))
              and all(abs((b - a) - 1) < 0.01 for a, b in zip(op.polls, op.polls[1:]) if a >= 30), op.polls)
        check("時間: 結果画面の「時間」を読む(長い場合)", info.get("inst_time") == "80.0", info)
        check("トリガー: 測定後に引かれたままなら離す", op.trig == "pull", op.trig)

        for shown, want in (("時間30.0", "30.0"), ("時間:30.0秒", "30.0"), ("時間 30,5", "30.5"), ("時間 30 s", "30")):
            _, info, _ = run_measure(30, 30, [shown])
            check(f"時間: 表記の揺れ「{shown}」→ {want}", info.get("inst_time") == want, info)

        _, info, _ = run_measure(30, 30, ["70500-01"])
        check("時間: 結果画面に「時間」が無ければ inst_time なし", "inst_time" not in info, info)

        try:
            run_measure(30, None, [])
            check("安全上限: 終わらなければ 表の時間+600秒で中断", False, "中断しなかった")
        except am.AbortError as e:
            check("安全上限: 終わらなければ 表の時間+600秒で中断", Clock.t >= 0 and "600" in str(e), e)

        # ---- 機器で測った回の一覧: 結果表 + 結果表に載らない回(Spectrometer Mode は pdz だけ。2026-11-13 実機)
        class FakeInst:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, path):
                return ("File #,Application,ElapsedTime\r\n796,Glass Dual, 20.0\r\nFile #,Application,ElapsedTime\r\n"
                        "798,Obsidian, 12.0\r\n").encode("utf-8-sig")
            def list(self, folder):
                E = lambda n, d=False: inf.Entry(n, 1, None, d)
                return [E("00796-Glass Dual.pdz"), E("00797-Spectrometer Mode.pdz"), E("00798-Obsidian.pdz"),
                        E("Results.csv"), E("00799-X.pdz", True), E("readme.pdz")]
        rr = inf.results_rows(connect=FakeInst)
        check("結果表: 見出しの繰り返しを除き、結果表に載らない pdz だけの回(Spectrometer Mode)を時間なしで足して No の順に並べる",
              [(r["File #"], r["Application"], r["ElapsedTime"]) for r in rr]
              == [("796", "Glass Dual", "20.0"), ("797", "Spectrometer Mode", ""), ("798", "Obsidian", "12.0")], rr)
        sp = am.resume_step(type("Op", (), {"state": lambda self: "login", "trigger_state": lambda self: "released"})(),
                            "Spectrometer Mode", 10, Clock.t, lambda no: [r for r in rr if int(r["File #"]) > no][:1], 796)
        check("再開: 結果画面が消えても、結果表に載らない回(pdz だけ)を測り終えた回として読む(時間は分からない → 接続エラー)",
              sp and sp.get("sample_no") == "797" and sp.get("inst_time") == ""
              and am.failure({"set_s": 10.0, "inst_time": "", "app": "Spectrometer Mode"}, True, "cut") == am.FAIL_CUT, sp)
        # ---- 「画面更新」: 途中の画面と結果画面はメイン画面へ戻す(テンキー・測定中は戻さない)
        class StopOp:
            def __init__(self, states):
                self.states = list(states)
            def state(self, ui=None):
                return self.states[0]
        real = am.to_main
        try:
            am.to_main = lambda op: op.states.__setitem__(0, "main")
            got = {st: am.back_to_main(StopOp([st])) for st in ("select_app", "confirm", "settings", "result", "keypad", "main")}
            check("画面更新: アプリ選択・構成確認・設定・結果画面はメイン画面へ戻す。テンキーは戻さない",
                  got == {"select_app": "main", "confirm": "main", "settings": "main",
                          "result": "main", "keypad": "keypad", "main": "main"}, got)
        finally:
            am.to_main = real
        check("停止は照射中だけ: 設定中の停止の後片付け(_abandon_settings・KEYPAD_CANCEL・finish_stop)は無い",
              not any(hasattr(am, k) for k in ("_abandon_settings", "KEYPAD_CANCEL", "finish_stop", "_stop_point")))
        # ---- ずれの判定は画面の時間を優先する
        check("ずれ: 画面の時間が同じなら、経過が長くてもずれなし",
              am.time_mismatch({"set_s": 30.0, "elapsed": 37.0, "inst_time": "30.0"}) is None)
        check("ずれ: 画面の時間が違えば、経過が近くてもずれあり",
              am.time_mismatch({"set_s": 30.0, "elapsed": 31.0, "inst_time": "26.0"}) == "設定 30 秒 / 機器 26 秒")
        check("ずれ: 画面の時間が読めないときだけ経過時間で判定(15秒超)",
              am.time_mismatch({"set_s": 30.0, "elapsed": 44.0}) is None
              and am.time_mismatch({"set_s": 30.0, "elapsed": 46.0}) is not None)

        # ---- run_plan: 計測毎に設定 オフ / オン
        calls, rows = [], []
        am.switch_app = lambda op, app: calls.append(("switch", app))
        am.set_phase_times = lambda op, app, ph: calls.append(("set", app))
        am.log = lambda row: None
        ops = iter([FakeOp(26, ["70600-01", "時間 26.0"]), FakeOp(75, ["70700-01", "時間 75.0"])])
        real_measure, real_sleep, real_main = am.measure, am._sleep, am.to_main
        am.measure = lambda op, s: (calls.append(("measure",)), real_measure(next(ops), s))[1]
        am._sleep = lambda t: (calls.append(("wait", t)) if t == am.SWITCH_WAIT_S else None,
                               setattr(Clock, "t", Clock.t + max(0.0, t)))
        am.to_main = lambda op: calls.append(("main",))
        try:
            am.run_plan(None, [("Obsidian", [30]), ("Glass Dual", [15, 60])], set_each=False,
                        on_result=lambda r: rows.append(r))
        finally:
            am.measure, am._sleep, am.to_main = real_measure, real_sleep, real_main
        check("切替前の待ち: 測定の後、次の切替・終了処理の前に2秒待つ",
              [c for c in calls if c[0] in ("measure", "wait", "switch", "main")] ==
              [("switch", "Obsidian"), ("measure",), ("main",), ("wait", 2),          # measure は最初にメイン画面を確かめる
               ("switch", "Glass Dual"), ("measure",), ("main",), ("wait", 2), ("main",)],
              calls)
        check("計測毎に設定オフ: 設定画面を開かない", ("set", "Obsidian") not in calls, calls)
        check("計測毎に設定オフ: ずれた測定だけ知らせる",
              [r.get("mismatch") for r in rows] == ["設定 30 秒 / 機器 26 秒", None], [r.get("mismatch") for r in rows])
        calls.clear()
        ops = iter([FakeOp(30, ["時間 30.0"])])
        am.measure = lambda op, s: real_measure(next(ops), s)
        try:
            am.run_plan(None, [("Obsidian", [30])], set_each=True)
        finally:
            am.measure = real_measure
        check("計測毎に設定オン: 測定の前に設定画面で時間を合わせる",
              calls == [("switch", "Obsidian"), ("set", "Obsidian")], calls)
        resume_tests()
        retry_tests()
        fetch_tests()
        safety_tests()
        path_tests()
        refs_tests()
        storage_tests()
        match_tests()
        device_tests()
        field_tests()
        exe_tests()
        detect_tests()
        checkbox_tests()
        settle_tests()
        lid_tests()
        trigger_log_tests()
        release_tests()
    except Exception:
        check("例外なし", False, traceback.format_exc())
    finally:
        am.time, am._sleep, am.to_main, am.say, am.progress, am.log, am.switch_app, am.set_phase_times = saved

    ng = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - ng}/{len(results)} OK")
    return 1 if ng else 0


if __name__ == "__main__":
    sys.exit(main())
