"""本体画面の中身(画面を持たない。web_app が使う。2026-11-19〜。決まりは前の Tk 版の gui_device.DevicePanel を書き写した)

  - USB は自動で探す(UI.DEVICE_SCAN_MS おき。機器には何も送らない)。見つかった機器の一覧・「接続」・案内の文(4 通り)
  - 1台見つかったら裏でつないで確かめる(_check): 製造番号が分かったら「接続可」。初期化の前・初期化中なら booting を立てて
    初期化の後の画面になるまで見続ける(_watch_boot)。「接続不可」なら UI.CHECK_RETRY_S あけて確かめ直す。2台以上なら確かめない
  - 「接続」: 確かめた接続をそのまま使う。画面が届くまでは「接続中…」。dl.IN_USE を立てる
  - 切れたら切れる直前の画面にグレーをかけて残し、一覧に戻す。同じ製造番号の機器が戻って「接続可」になれば自動でつなぎ直す
画面の側(view)は、一覧・本体画面・「接続中…」を映すだけ。時間を置いて呼ぶのは after(ms, 関数)(WebView2 版は画面の糸で行う)。
テストは、このモジュールの connect_link / find_devices / screen_state を差し替える
"""
from __future__ import annotations

import queue, threading, time

import device_link as dl
from gui_config import UI

# つなぐ関数・見つかった機器・届いている画面の状態(テストでは差し替える)
connect_link = lambda host=None, source=None, refused_wait=None: dl.shared_link(connect=True, host=host, source=source,
                                                                                refused_wait=refused_wait)
find_devices = lambda: dl.find_s1_titan(pending=True)


def screen_state(link: dl.DeviceLink) -> str:
    import auto_measure as am, screen_match
    return am.screen_state(screen_match.library().read(link.snapshot()))


def device_ready(link: dl.DeviceLink) -> bool:
    """「接続」を押せるか: 画面の接続(55102)がつながって、機器の情報(製造番号)が届いている(画面はまだでもよい)"""
    return link.connected and getattr(link, "info_at", 0) > 0


BEFORE_INIT_END = ("booting", "unknown")    # 初期化が終わったとは言えない画面(初期化中・見本に無い画面 = 起動の前の OS の画面など)


def mark_boot(link: dl.DeviceLink, note) -> str:
    """届いた画面を見本で読み、初期化の前・初期化中なら booting を立てて、初期化の後の画面になるまで裏で見る。読んだ画面の状態を返す"""
    if not hasattr(link, "snapshot") or not getattr(link, "seq", 0):
        return "?"
    st = screen_state(link)
    if st in BEFORE_INIT_END and not link.booting:
        link.booting = True
        threading.Thread(target=_watch_boot, args=(link, note), daemon=True).start()
    return st


def _watch_boot(link: dl.DeviceLink, note, limit: float = 0.0):
    last, end = -1, time.time() + (limit or UI.CHECK_MAX_S)
    while link.connected and link.booting and time.time() < end:
        if link.seq != last:
            last = link.seq
            try:
                st = screen_state(link)
            except Exception:
                st = "unknown"
            if st not in BEFORE_INIT_END:
                link.booting, link.booted_at = False, time.time()
                note(f"確認: 初期化の後の画面({st})になった(画面の接続から {time.time() - link.connected_at:.1f} 秒)")
                return
        time.sleep(UI.BOOT_WATCH_S)
    link.booting = False


def _safe_list(f) -> list:
    try:
        return list(f())
    except Exception:
        return []


class DeviceWatch:
    """本体画面の中身。view(画面の側)は次を持つ:
      show_list(rows, selected, message, can_connect)  一覧(製造番号・IP・状態)と案内の文と「接続」を押せるか
      show_overlay(on)                                 一覧・「接続」・案内の文を本体画面の上に出す / 消す
      show_wait(on)                                    白い本体画面の真ん中に「接続中…」
      redraw()                                         本体画面を描き直す(frame() の絵)
    呼び返し: alert(タイトル, 説明)・on_busy(つないでいるか)・on_connected()・on_lost()・on_trigger()・on_booted()・note(文)"""

    def __init__(self, view, after, alert=lambda t, d: None, on_busy=lambda busy: None, on_connected=lambda: None,
                 on_lost=lambda: None, on_trigger=lambda: None, on_booted=lambda: None, note=lambda msg: None):
        self.view, self.after = view, after
        self.alert, self.on_busy, self.note = alert, on_busy, note
        self.on_connected, self.on_lost, self.on_trigger, self.on_booted = on_connected, on_lost, on_trigger, on_booted
        self.closing = False
        self.connecting = False
        self.found: list[dict] = []                    # 見つかった機器(key・host・source・alias・serial・phase)
        self.selected: str | None = None               # 一覧で選んでいる機器の key
        self._gen = 0                                  # 確かめる処理の世代(機器が変わる・つなぐと前の処理は止まる)
        self._checking = None                          # 確かめている機器の alias(同時に1つだけ)
        self._q: queue.Queue = queue.Queue()
        self.link: dl.DeviceLink | None = None
        self.manual = True                             # 手での操作を受け付けるか(自動測定の間は False)
        self._shown = -1
        self._lost_told = False
        self.last = None                               # 切れる直前の画面(切れている間、グレーをかけて残す)
        self.auto_serial = ""                          # 「接続」でつないだ機器の製造番号(同じ機器が戻れば自動でつなぎ直す)
        self._booted = 0.0
        self._trig = None

    def start(self):
        self._show()
        self.view.show_overlay(True)
        self.after(100, self._poll_check)
        self.after(10, self.scan)

    # ---- 表示
    def frame(self):
        """本体画面に出す絵(PIL の 480x640)と、グレーをかけるか: (絵 | None, 切れている間か)"""
        link = self.link
        if link is not None and getattr(link, "frame", None) is not None and getattr(link, "seq", 0):
            return link.snapshot(), False
        if self.last is not None:
            return self.last, True
        return None, False

    def chosen(self) -> dict | None:
        return next((d for d in self.found if d["key"] == self.selected), None)

    def select(self, key: str | None):
        self.selected = key if any(d["key"] == key for d in self.found) else None
        self._show()

    def _can_connect(self) -> bool:
        d = self.chosen()
        return d is not None and d.get("phase") in ("ready", "multi") and not self.connecting

    def _show(self):
        """見つかった機器を一覧に出し(1台なら選んでおく)、案内の文を状況に合わせる(4 通り)"""
        if not any(d["key"] == self.selected for d in self.found):
            self.selected = self.found[0]["key"] if len(self.found) == 1 else None
        phases = {d["phase"] for d in self.found}
        msg = (UI.DEVICE_CHECKING if self.connecting else UI.DEVICE_NONE if not self.found
               else UI.DEVICE_READY if phases & {"ready", "multi"} else UI.DEVICE_CHECKING if "booting" in phases
               else UI.DEVICE_FAILED)
        rows = [{"key": d["key"], "serial": d.get("serial") or UI.SERIAL_CHECKING, "host": d.get("host") or UI.SERIAL_CHECKING,
                 "state": UI.DEVICE_ROW_STATE[d["phase"]]} for d in self.found]
        self.view.show_list(rows, self.selected, msg, self._can_connect())

    # ---- 自動の検索・確認(つながっていない間)
    def scan(self):
        if self.closing:
            return
        if not (self.alive() or self.connecting):
            devs = {d["alias"]: d for d in _safe_list(lambda: find_devices())}
            before = [(d["key"], d["phase"]) for d in self.found]
            for d in [d for d in self.found if d["alias"] not in devs]:          # USB が外れた
                self.found.remove(d)
                if self._checking == d["alias"]:
                    self._gen += 1; self._checking = None
                self.note(f"検索: {d['alias']} が外れました")
            known = {d["alias"] for d in self.found}
            for a, d in devs.items():                                          # USB が現れた
                if a not in known:
                    self.found.append(dict(d, key=a, phase="booting", serial=""))
                    self.note(f"検索: {a} が見つかりました(機器 {d['host'] or 'アドレス待ち'}・PC 側 {d['source'] or '-'})")
            multi = len(self.found) > 1
            for d in self.found:
                if multi:
                    if self._checking:
                        self._gen += 1; self._checking = None
                    d["phase"] = "multi"
                elif d["phase"] == "multi":
                    d["phase"] = "booting"
                elif d["phase"] == "ready" and not (dl.LINK is not None and dl.LINK.connected):
                    d["phase"] = "booting"                     # 確かめた接続が切れた(USB はある): 確かめ直す
                    self.note(f"検索: {d.get('serial') or '?'} {d['alias']} の接続が切れたので確かめ直します")
                elif d["phase"] == "failed" and time.time() >= d.get("retry_at", 0):
                    d["phase"] = "booting"
                if d["phase"] == "booting" and self._checking is None and not multi:
                    self._checking = d["alias"]
                    threading.Thread(target=self._check, args=(self._gen, dict(d)), daemon=True).start()
            if before != [(d["key"], d["phase"]) for d in self.found] or not self.found:
                self._show()
        self.after(UI.DEVICE_SCAN_MS, self.scan)

    def _check(self, gen: int, d: dict):
        """(裏)見つかった機器を確かめる。「接続可」・「接続不可」・USB が外れるで終わる"""
        end, err, link = time.time() + UI.CHECK_MAX_S, "", None
        t0, was_up = time.time(), False
        alive = lambda: gen == self._gen and not self.closing
        log = lambda m: self.note(f"確認: {m}({time.time() - t0:.1f} 秒)")
        while alive() and time.time() < end:
            if was_up and link is not None and not link.connected:
                log(f"画面の接続が切れた({link.error or '?'})")
                was_up = False
            if not d["host"]:                           # USB が現れてから約 3 秒はアドレスが無い
                cur = next((x for x in _safe_list(lambda: find_devices()) if x["alias"] == d["alias"]), None)
                if cur and cur["host"]:
                    d.update(host=cur["host"], source=cur["source"])
                    log("アドレスがついた")
                    self._q.put((gen, d["key"], {"host": d["host"], "source": d["source"]}))
                else:
                    time.sleep(UI.CHECK_INTERVAL_S)
                continue
            try:
                told = False
                if link is None or not link.connected:
                    log("つなぎ始め")
                    box: list = []

                    def job():
                        try:
                            box.append(connect_link(d["host"], d["source"], refused_wait=max(1.0, end - time.time())))
                        except Exception as e:
                            box.append(e)
                    th = threading.Thread(target=job, daemon=True)
                    th.start()
                    while th.is_alive():        # つないでいる途中: 製造番号が分かった時点で「接続可」(画面はまだでもよい)
                        lk = dl.LINK
                        if not told and lk is not None and alive() and device_ready(lk):
                            told = True
                            log(f"製造番号が分かった({lk.name}) → 接続可")
                            self._q.put((gen, d["key"], {"serial": lk.name, "phase": "ready"}))
                        th.join(0.1)
                    if isinstance(box[0], Exception):
                        raise box[0]
                    link, was_up = box[0], True
                    log("画面の接続がつながった(画面が届いた)")
                    self._q.put((gen, d["key"], {"serial": link.name}))
                if device_ready(link):
                    st = mark_boot(link, self.note)
                    if not told:
                        log(f"製造番号が分かった({link.name}) → 接続可")
                        self._q.put((gen, d["key"], {"phase": "ready"}))
                    log(f"画面: {st}")
                    self._q.put((gen, d["key"], {"judged": st}))
                    return
            except Exception as e:
                err = str(e)
                log(f"つながらない({type(e).__name__}: {e})")
                time.sleep(1.0)
                continue
            time.sleep(UI.CHECK_INTERVAL_S)
        if alive():
            self._q.put((gen, d["key"], {"phase": "failed", "error": err or "時間切れ", "retry_at": time.time() + UI.CHECK_RETRY_S}))

    def _poll_check(self):
        try:
            while True:
                gen, key, v = self._q.get_nowait()
                d = next((x for x in self.found if x["key"] == key), None)
                if gen != self._gen or d is None or self.alive():
                    continue
                d.update(v)
                ph = d["phase"]
                if "judged" in v and self.last is not None:    # つなぎ直して初期化中かを判断できた: 切れる前の画面を消して白に
                    self.last = None
                    self.view.redraw()
                if ph in ("ready", "failed") and "phase" in v:
                    self._checking = None
                    self.note(f"検索: {d.get('serial') or '?'} {d['host']}・{d['alias']} → "
                              + ("接続可" if ph == "ready" else f"接続不可({d.get('error')})"))
                self._show()
                if (ph == "ready" and "phase" in v and self.auto_serial and d.get("serial") == self.auto_serial
                        and len(self.found) == 1 and not (self.alive() or self.connecting)):
                    self.note(f"接続: 同じ機器({d['serial']})が戻ったので自動でつなぎ直す")
                    self.connect(d["host"], d["source"])
        except queue.Empty:
            pass
        if not self.closing:
            self.after(100, self._poll_check)

    # ---- 接続
    def connect_found(self) -> bool:
        """「接続」: 一覧で選んだ機器につなぐ(確かめたときの接続をそのまま使う)"""
        d = self.chosen()
        if d and d.get("phase") in ("ready", "multi") and not (self.alive() or self.connecting):
            self.connect(d["host"], d["source"])
            return True
        return False

    def connect(self, host: str | None = None, source: str | None = None, on_ready=lambda: None):
        self._gen += 1
        self._checking = None
        self.connecting = True
        self.on_busy(True)
        self._show()
        self.view.show_overlay(False)
        self.view.show_wait(True)
        result = []
        t0 = time.time()

        def job():
            try:
                result.append(connect_link(host, source))
            except Exception as e:
                result.append(e)
        threading.Thread(target=job, daemon=True).start()

        def poll():
            if not result:
                self.after(200, poll); return
            self.connecting = False
            self.on_busy(False)
            self.view.show_wait(False)
            if isinstance(result[0], Exception):
                self.link = None
                self.note(f"接続: つながらない({result[0]})")
                self.alert("機器につながりません", f"{result[0]}。{UI.RECONNECT_NOTE}")
                for d in self.found:
                    d.update(phase="failed", retry_at=time.time() + UI.CHECK_RETRY_S)
                self.view.show_overlay(True)
                self._show()
            else:
                self._connected(result[0], t0)
            on_ready()
        self.after(200, poll)

    def _connected(self, link: dl.DeviceLink, t0: float):
        self.view.show_overlay(False)
        alias = next((d["alias"] for d in _safe_list(dl.find_s1_titan) if d["source"] == link.source), "?")
        self.note(f"接続: 機器につながりました(製造番号 {link.name or '?'}・{link.host}・PC 側 {link.source or '?'}"
                  f"・{alias}・PC 側のポート {link.local_port}・つなぐのに {time.time() - t0:.1f} 秒)")
        self.link, self._lost_told, self._shown = link, False, -1
        self.auto_serial, self.last = link.name, None
        dl.IN_USE = True
        try:
            st = mark_boot(link, self.note)
        except Exception as e:
            st = f"? ({e})"
        self.note(f"接続: 画面 {st}")
        self._tick()
        self.on_connected()

    def alive(self) -> bool:
        link = self.link
        return link is not None and not link.error and link.connected

    def close(self):
        self.closing = True
        self._gen += 1
        dl.close_link()

    def _lost(self):
        link = self.link
        if link is not None and getattr(link, "frame", None) is not None and getattr(link, "seq", 0):
            self.last = link.snapshot()
        self.link = None
        dl.IN_USE = False
        self.view.redraw()
        self.found = []
        self._show()
        self.view.show_overlay(True)
        self.on_lost()

    def _tick(self):
        link = self.link
        if link is None or self.closing:
            return
        if link.error:
            if not self._lost_told:
                self._lost_told = True
                self.note(f"接続: 切れました({link.error})")
                self.alert("機器との接続が切れました", f"{link.error}。{UI.RECONNECT_NOTE}")
                self._lost()
            return
        if link.seq == 0 and link.connected_at and time.time() - link.connected_at > UI.NO_FRAME_RECONNECT_S:
            self.note(f"接続: 画面が {UI.NO_FRAME_RECONNECT_S} 秒届かないので切りました")
            dl.close_link()
            self._lost()
            return
        if link.seq != self._shown:
            self._shown = link.seq
            self.view.redraw()
        if getattr(link, "booted_at", 0) > self._booted:
            self._booted = link.booted_at
            self.on_booted()
        if link.status != self._trig:
            self._trig = link.status
            if link.status is not None:
                self.on_trigger()
        self.after(UI.DEVICE_REFRESH_MS, self._tick)

    def click(self, x: int, y: int) -> bool:
        """本体画面のクリック(機器の px)を機器へ送る(手での操作。自動測定の間は受け付けない)"""
        link = self.link
        if not self.manual or link is None or not link.connected:
            return False
        threading.Thread(target=lambda: _quiet(link.click, x, y), daemon=True).start()
        return True


def _quiet(f, *a):
    try:
        f(*a)
    except Exception:
        pass
