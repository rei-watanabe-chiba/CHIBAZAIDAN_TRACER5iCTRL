"""機器へ直接つなぐ(RemoteCTRL を使わない)。通信の形は PROTOCOL_55100.md(2026-11-10 に中継で記録して解析)

  55102: 機器の画面(480x640、RGB565 の色の連続の圧縮)を受け取る / クリックを送る(6 バイト)
  55101: トリガーの状態の知らせを受け取る(TRIGGER_RELEASED / NOT_READY / PULLED)
  55100: トリガーの命令を送る(TRIGGER_PULL / TRIGGER_RELEASE。返事 OK)。**TRIGGER_PULL は X線照射**

DirectRemote は bruker_ui.RemoteCtrl と同じ使い方(capture_background / read_ui / to_client / click /
trigger_state / trigger)ができるので、auto_measure.Operator はどちらでも動く。画面は見本との照合で読む(OCR なし)。
機器は同時に1つの相手としかつながらない(このアプリがつないでいる間は RemoteCTRL はつなげない)。
"""
from __future__ import annotations

import ctypes, os, socket, struct, threading, time

from PIL import Image

import screen_match
from bruker_ui import REF_W, ScreenUI

HOST = "192.168.137.139"
PORT_SCREEN, PORT_STATUS, PORT_COMMAND = 55102, 55101, 55100
HELLO = bytes.fromhex("01040100")      # 機器の情報を受け取った直後に返す(RemoteCTRL と同じ)
INFO_LEN = 332
UPDATE, ALIVE = 0x14, 0x19
DOWN, UP = 2, 4                        # クリック: 押す / 離す
STATUS = {"TRIGGER_RELEASED": "released", "TRIGGER_NOT_READY": "not_ready", "TRIGGER_PULLED": "pulled"}
STATUS_WAIT_S = 3.0                    # つないだ後、トリガーの状態の最初の知らせを待つ上限
CONNECT_TIMEOUT_S = 10.0               # 接続(TCP)の返事を待つ上限
SUB_CONNECT_TIMEOUT_S = 3.0            # 55101・55100 の接続の返事を待つ上限(機器とは約 1 ms で返る。断られるときは Windows が試し直して
                                       # 約 2.0〜2.4 秒かかるので、それより長く。2026-11-13 実機: 初期化中は返事が無いことがあり、10 秒待つと
                                       # 起動画面の後も 1〜4 秒つながらなかった)
SUB_RETRY_S = 3.0                      # トリガーの接続(55101・55100)が切れた・つながらないとき、つなぎ直すまでの間(画面の接続は保つ)
SUB_REFUSED_RETRY_S = 1.0              # トリガーの接続を断られたとき(機器の初期化の途中。55102 だけが開いている)、試し直すまでの間
                                       # (断られても接続は残らないので、短くても機器をふさがない。2026-11-12: 3 → 1 秒で「接続可」が最大 約 2 秒早く)
FIRST_FRAME_S = 10.0                   # 挨拶の後、最初の画面を待つ上限(届いてから 55101・55100 をつなぐ。実機では約 1.7 秒)
INFO_WAIT_S = 60.0                     # つないだ後、機器の情報を待つ上限(起動の途中は届くまで時間がかかる。切らずに待つ)
FRAME_WAIT_S = 10.0                    # 画面を読むとき、まだ1枚も届いていなければこの秒数まで待つ(来なければ「画面が届きません」)


def _rgb565(col: int) -> tuple[int, int, int]:
    return ((col >> 11) & 31) * 255 // 31, ((col >> 5) & 63) * 255 // 63, (col & 31) * 255 // 31


def apply_update(img: Image.Image, rect: tuple[int, int, int, int], data: bytes):
    """画面の更新(範囲 rect に、(個数, 色) の並び)を img に描く"""
    l, t, r, b = rect
    px = img.load()
    x, y = l, t
    for i in range(0, len(data) - 3, 4):
        cnt, col = struct.unpack_from("<HH", data, i)
        rgb = _rgb565(col)
        for _ in range(cnt):
            if y >= b:
                return
            px[x, y] = rgb
            x += 1
            if x >= r:
                x, y = l, y + 1


class DeviceLink:
    """機器への3本の接続。画面と状態は受信用のスレッドが更新し続ける。送信はロックで1つずつ"""

    def __init__(self, host: str = HOST, timeout: float = 10.0,
                 ports: tuple[int, int, int] = (PORT_SCREEN, PORT_STATUS, PORT_COMMAND), source: str | None = None):
        """source: 送り元の PC 側のアドレス(その機器の USB の接続のもの。決めると、その接続だけを通る)"""
        self.host, self.timeout, self.source = host, timeout, source
        self.ports = dict(zip(("screen", "status", "command"), ports))   # テストでは偽の機器の番号にする
        self.width = self.height = 0
        self.name = ""                              # 製造番号(例 900G12901)
        self.frame: Image.Image | None = None
        self.seq = 0                                # 画面の更新の回数(更新を待つのに使う)
        self.status: str | None = None              # released / not_ready / pulled
        self.status_at = 0.0
        self.last_alive = 0.0
        self.connected_at = 0.0                     # つないだ時刻(画面が来ないままのつなぎ直しに使う)
        self.sub_error = ""                         # トリガーの接続が切れている理由(つなぎ直したら空。画面の接続は保っている)
        self.error = ""
        self.local_port = 0                         # 画面の接続の PC 側のポート番号(つなぎ直すとき同じ番号を使う。下の connect)
        self.sub_ports: dict[str, int] = {}         # 55101・55100 の PC 側のポート番号(同じく、つなぎ直すとき同じ番号を使う)
        self.booted_at = 0.0                        # 初期化の後の画面になったと使う側が気づいた時刻(device_core._watch_boot)
        self.booting = False                        # 機器が初期化中(画面で判断するのは使う側。立てると、黙っても BOOT_ALIVE_TIMEOUT_S まで切らずに待つ)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self._release_lock = threading.Lock()       # 止める命令を 2 つの糸から同時に送らない(蓋の糸と測定の糸)
        self.release_sent = 0.0                     # 止める命令を送った時刻 / OK が返った時刻(遅れの内訳をログに残す)
        self.release_ok = 0.0
        self._replies: list[str] = []
        self._socks: dict[str, socket.socket] = {}

    # ---- 接続
    @property
    def connected(self) -> bool:
        return bool(self._socks) and not self.error

    def connect(self):
        """つなぐ(55102 → 情報 → 挨拶 → 最初の画面 → 55101 / 55100)。失敗したら OSError。
          - 55102 は、前につないだことがあれば前と同じ PC 側のポート番号でつなぐ(local_port)。2026-11-12 実機: USB を抜いたとき
            機器の情報を待つ途中だった接続は機器に残り、新しい番号では 3〜6 分断られ続けたが、同じ番号なら機器が前の接続を
            片付けて 1 回目で受け付けた(安定してからでも、別の USB の口に差し直しても同じ。PC 側のアドレスは変わらなかった)
          - 起動の途中の機器は、接続は受け付けても情報を送ってくるまで時間がかかる: 切らずに同じ接続のまま INFO_WAIT_S まで待つ
            (待ちきれずに切ってつなぎ直すと、機器は前の接続の後始末が終わるまで次の接続に答えない)
          - 55101・55100 は1回だけ試し、だめなら裏で試し続ける(_sub_retry)。画面の接続は切らない(機器の初期化の途中は
            55102 だけが開いていて、ほかは断られる。初期化の終わりに 55102 は約 7 秒黙り、同じ接続のまま起動画面が届く。
            ほかはそのころに開く)"""
        self.close(abort=True)
        self.error, self.status, self.sub_error, self.seq, self.booting = "", None, "", 0, False
        self.info_at = self.booted_at = 0.0
        t_open = time.time()
        s = self._open(self.ports["screen"], local=self.local_port)
        NOTE(f"[時間] 55102 が受け付けた({time.time() - t_open:.2f} 秒・PC 側のポート {s.getsockname()[1]}"
             f"{'・前と同じ' if self.local_port and s.getsockname()[1] == self.local_port else ''})")
        self.local_port = s.getsockname()[1]
        t_info = time.time()
        s.settimeout(INFO_WAIT_S)
        try:
            info = self._recv_exact(s, INFO_LEN)
        except OSError:
            _abort(s)
            raise
        self.width, self.height, depth = struct.unpack_from("<3I", info, 8)
        if depth != 16 or not (0 < self.width <= 2000 and 0 < self.height <= 2000):
            _abort(s)
            raise ConnectionError(f"想定と違う画面です({self.width}x{self.height} {depth}bit)")
        names = [x.decode("ascii", "replace") for x in info[36:].split(b"\0") if len(x) > 2]
        self.name = names[-1] if names else ""
        self.frame = Image.new("RGB", (self.width, self.height), "white")
        s.sendall(HELLO)
        s.settimeout(None)
        self._socks["screen"] = s
        self.last_alive = self.connected_at = self.info_at = time.time()   # 製造番号が分かった(「接続可」はここから)
        NOTE(f"[時間] 55102 の機器の情報が届いた(待ち {self.info_at - t_info:.2f} 秒)")
        threading.Thread(target=self._screen_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        # 最初の画面が届いてから 55101・55100 をつなぐ。届く前につなぐと、機器はトリガーの状態の接続(55101)をすぐ切る
        # (2026-11-12 実機で比べた: 挨拶の直後につなぐと 1.7 秒で切られ、状態の知らせも来ない。画面の後なら切られず知らせも来る)
        if not self.wait_frame(0, FIRST_FRAME_S):
            self.close(abort=True)
            raise ConnectionError(self.error or "機器の画面が届きません")
        NOTE(f"[時間] 55102 の最初の画面が届いた(情報から {time.time() - self.info_at:.2f} 秒)")
        # 55101・55100 は裏でつなぐ(待たずに返し、すぐ画面を出せるように。初期化中は 1 回に最大 SUB_CONNECT_TIMEOUT_S かかる)。
        # トリガーの状態の知らせは、ここでは待たない(照射できるかを判断するときに待つ: DirectRemote.trigger_state)
        socks = self._socks
        for key in ("status", "command"):
            threading.Thread(target=self._sub_retry, args=(key, socks, False, True), daemon=True).start()

    def _open_sub(self, key: str) -> socket.socket:
        """55101・55100 を、前につないだことがあれば前と同じ PC 側のポート番号でつなぐ(55102 と同じ理由。2026-11-12 実機:
        USB を差し直すと、新しい番号では 55101 が約 3 秒断られ続けた。機器に前の接続が残っていたとみられる)"""
        c = self._open(self.ports[key], local=self.sub_ports.get(key, 0), timeout=SUB_CONNECT_TIMEOUT_S)
        self.sub_ports[key] = c.getsockname()[1]
        return c

    def _open(self, port: int, local: int = 0, timeout: float = CONNECT_TIMEOUT_S) -> socket.socket:
        """機器のポートへつなぐ(送り元を決めていれば、その接続だけを通る)。local を決めると PC 側のポート番号もそれにする
        (使えなければ新しい番号で。断られた・返事が無いときは、そのまま失敗にする)"""
        if local:
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                c.bind((self.source or "", local))
            except OSError:
                c.close()
            else:
                c.settimeout(timeout)
                try:
                    c.connect((self.host, port))
                    return c
                except OSError as e:
                    _abort(c)
                    if getattr(e, "winerror", None) not in (10048, 10049):   # その番号が PC 側でまだ使えない: 新しい番号で
                        raise
        return socket.create_connection((self.host, port), timeout=timeout,
                                        source_address=(self.source, 0) if self.source else None)

    def close(self, abort: bool = False):
        """接続を閉じる。abort=True(切れた・失敗したとき)はすぐ捨てる(リセット。PC 側にその番号の名残を残さず、
        同じ番号でつなぎ直せるように)。アプリを閉じるときなどはふつうに閉じる(機器が早く片付けられるように)"""
        socks, self._socks = self._socks, {}
        for s in socks.values():
            if abort:
                _abort(s)
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        with self._changed:
            self._changed.notify_all()

    # ---- 受信
    @staticmethod
    def _recv_exact(s: socket.socket, n: int) -> bytes:
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("機器との接続が切れました")
            b += c
        return b

    def _fail(self, e: Exception):
        """画面の接続が切れた: 理由を残し、3本ともすぐ捨てる(同じ番号でつなぎ直せるように)"""
        if self._socks and not self.error:
            self.error = str(e) or type(e).__name__
            self.status = None                      # トリガーの状態は分からなくなる(前の知らせを使わない)
            self.close(abort=True)
        with self._changed:
            self._changed.notify_all()

    def _screen_loop(self):
        s = self._socks.get("screen")
        try:
            while s is not None:
                typ = struct.unpack("<I", self._recv_exact(s, 4))[0]
                now = time.time()
                if now - self.last_alive > GAP_NOTE_S:      # 知らせが長く止まった後に、同じ接続でまた届いた(初期化の終わりなど)
                    NOTE(f"[時間] 55102 の知らせが {now - self.last_alive:.1f} 秒止まって、同じ接続でまた届いた(種類 {typ:#x})")
                self.last_alive = now                       # 画面の更新も、機器が生きている知らせとして数える
                if typ == ALIVE:
                    continue
                if typ != UPDATE:
                    raise ConnectionError(f"知らない画面のデータ(種類 {typ:#x}・{s.recv(64, socket.MSG_PEEK)[:32].hex()})")
                n, l, t, r, b = struct.unpack("<5I", self._recv_exact(s, 20))
                data = self._recv_exact(s, n * 2)
                with self._changed:
                    apply_update(self.frame, (l, t, min(r, self.width), min(b, self.height)), data)
                    self.seq += 1
                    self._changed.notify_all()
        except (OSError, ConnectionError, struct.error) as e:
            self._fail(self._where(e, "画面"))

    def _where(self, e: Exception, which: str) -> Exception:
        """切れた理由に、どの接続か・つないでから何秒かを足す(後から調べるため)"""
        return ConnectionError(f"{e}({which}・つないで {time.time() - self.connected_at:.1f} 秒)")

    def _watchdog(self):
        """機器は約 1.1 秒ごとに生存の知らせ(0x19)を送る。ALIVE_TIMEOUT_S 来なければ切れたとみなす
        (USB を抜いたときなど、エラーが返らずに通信が黙って途絶えると、受信を待ったまま気づけないため)。
        初期化中(booting)は BOOT_ALIVE_TIMEOUT_S まで切らない: 初期化の終わりに機器は約 7 秒黙り、同じ接続のまま起動画面を送ってくる
        (2026-11-13 実機。RemoteCTRL も切らずに映し続けた。切ってつなぎ直すと、機器の情報を待つ約 5 秒の分だけ遅れていた)"""
        socks = self._socks
        while self._socks is socks and socks and not self.error:
            limit = BOOT_ALIVE_TIMEOUT_S if self.booting else ALIVE_TIMEOUT_S
            if time.time() - self.last_alive > limit:
                self._fail(ConnectionError(f"機器からの知らせが {limit:g} 秒届きません"
                                           + ("(初期化中)" if self.booting else "")))   # 受信を待っている処理も止まる
                return
            time.sleep(0.25)

    def _lines(self, key: str):
        s, buf = self._socks.get(key), b""
        while s is not None:
            c = s.recv(256)
            if not c:
                raise ConnectionError("機器との接続が切れました")
            buf += c
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                yield line.strip().decode("ascii", "replace")

    def _status_loop(self):
        try:
            for line in self._lines("status"):
                if line in STATUS:
                    with self._changed:
                        prev = self.status
                        self.status, self.status_at = STATUS[line], time.time()
                        self._changed.notify_all()
                    if self.status != prev:                 # 変わったときだけ(NOT_READY は約 0.6 秒ごとに繰り返し届く)
                        NOTE(f"[トリガー] {prev} → {self.status}({ms_stamp(self.status_at)})")
        except (OSError, ConnectionError) as e:
            self._sub_lost("status", self._where(e, "トリガーの状態"))

    def _command_loop(self):
        try:
            for line in self._lines("command"):
                with self._changed:
                    self._replies.append(line)
                    self._changed.notify_all()
        except (OSError, ConnectionError) as e:
            self._sub_lost("command", self._where(e, "命令"))

    def _sub_lost(self, key: str, e: Exception):
        """トリガーの接続(55101 の状態 / 55100 の命令)が切れた: 画面の接続は保ち(本体画面は出し続ける)、
        その接続だけを SUB_RETRY_S 後につなぎ直す。それまでトリガーの状態は不明(照射できるか分からないので測定は始めない)。
        2026-11-12 実機: 電源を入れた直後などに、機器がトリガーの状態の接続をつないだ直後に切ることがあった"""
        socks = self._socks
        s = socks.pop(key, None)
        if s is None or not socks or self.error:          # 画面の接続も無い(閉じた・切れた)
            return
        _abort(s)                                            # 同じ番号でつなぎ直せるよう、名残を残さない
        with self._changed:
            if key == "status":
                self.status = None
            self.sub_error = str(e)
            self._changed.notify_all()
        threading.Thread(target=self._sub_retry, args=(key, socks), daemon=True).start()

    def _sub_retry(self, key: str, socks: dict, refused: bool = False, first: bool = False):
        """トリガーの接続を、画面の接続がある間つなぐ・つなぎ直す(first なら待たずに試す。SUB_RETRY_S おき。
        断られた・返事が無いときは SUB_REFUSED_RETRY_S おき)"""
        n, errs = 0, set()
        while self._socks is socks and socks and not self.error:
            if not first:
                time.sleep(SUB_REFUSED_RETRY_S if refused else SUB_RETRY_S)
            first = False
            if self._socks is not socks or not socks or self.error:
                return
            n += 1
            try:
                c = self._open_sub(key)
            except OSError as e:
                refused = isinstance(e, (ConnectionRefusedError, TimeoutError))   # 返事が無いときも、断られたときと同じ間隔で
                errs.add(type(e).__name__)
                self.sub_error = f"{key}: {e}"
                continue
            if self._socks is not socks or not socks or self.error:   # つないでいる間に画面の接続が閉じた: 残さない
                _abort(c)
                return
            c.settimeout(None)
            socks[key] = c
            self.sub_error = ""
            NOTE(f"[時間] {self.ports[key]} がつながった(画面の接続から {time.time() - self.connected_at:.1f} 秒・"
                 f"{n} 回目{'・' + '/'.join(sorted(errs)) if errs else ''})")
            threading.Thread(target=self._status_loop if key == "status" else self._command_loop, daemon=True).start()
            return

    # ---- 画面
    def wait_frame(self, after_seq: int, timeout: float) -> bool:
        """更新の回数が after_seq を超えるまで待つ"""
        with self._changed:
            return self._changed.wait_for(lambda: self.seq > after_seq or bool(self.error), timeout) and not self.error

    def snapshot(self) -> Image.Image:
        with self._lock:
            return self.frame.copy()

    # ---- 送信
    def _send(self, key: str, data: bytes):
        s = self._socks.get(key)
        if s is None or self.error:
            raise ConnectionError(self.error or ("トリガーの接続が切れています(つなぎ直し中)" if self._socks else "機器につながっていません"))
        with self._send_lock:
            s.sendall(data)

    def click(self, x: int, y: int):
        """機器の画面の (x, y) を押して離す"""
        x, y = max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y))
        self._send("screen", struct.pack("<3H", DOWN, x, y))
        time.sleep(0.08)
        self._send("screen", struct.pack("<3H", UP, x, y))

    def command(self, text: str, timeout: float = 5.0) -> bool:
        """55100 に命令を送り、OK が返れば True"""
        with self._changed:
            self._replies.clear()
        self._send("command", text.encode("ascii") + b"\r")
        with self._changed:
            self._changed.wait_for(lambda: bool(self._replies) or bool(self.error), timeout)
            return "OK" in self._replies

    def wait_status(self, want: tuple[str, ...], timeout: float) -> bool:
        with self._changed:
            return self._changed.wait_for(lambda: self.status in want or bool(self.error), timeout) and self.status in want

    def pull(self) -> bool:
        """**X線照射を始める**。照射できる状態(released)のときだけ送る。照射中になったら True"""
        if self.status != "released":
            raise RuntimeError(f"照射できる状態ではありません(状態: {self.status})")
        return self.command("TRIGGER_PULL") and self.wait_status(("pulled",), 5)

    def release(self) -> bool:
        """照射を止める。止まった(照射中でなくなった)ら True。照射中でなければ何もしない(照射を始める方へは決して送らない)。
        蓋の糸と測定の糸の両方から呼ばれる: 片方が送って RELEASE_AGAIN_S たたないうちは送り直さず、知らせを待つだけ"""
        if self.status != "pulled":
            return True
        with self._release_lock:
            if self.status == "pulled" and time.time() - self.release_sent > RELEASE_AGAIN_S:
                self.release_sent, self.release_ok = time.time(), 0.0
                if self.command("TRIGGER_RELEASE"):
                    self.release_ok = time.time()
        return self.wait_status(("released", "not_ready"), 5)


def _abort(s: socket.socket):
    """接続をすぐ捨てる(リセット)。PC 側にその番号の名残を残さない"""
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


LINK: DeviceLink | None = None     # アプリで1つの接続(測定のたびにつなぎ直さない。つなぐのに約5秒かかる)
_LINK_LOCK = threading.Lock()      # 画面の枠と状態の確認が同時につなごうとしても、つなぐのは1回


NOTE = lambda msg: None   # 接続の様子をログのファイルにだけ残す(GUI が storage.append_log にする)
RELEASE_AGAIN_S = 3.0     # 止める命令を送ってから、送り直さずに知らせを待つ時間(機器は OK まで約 0.6 秒・知らせまで約 1 秒)


def ms_stamp(t: float) -> str:
    """時刻をミリ秒まで(ログのファイルの行の時刻は秒までなので、トリガー・蓋の変化に添える)"""
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
ALIVE_TIMEOUT_S = 5.0     # 生存の知らせ(約 1.1 秒ごと)がこの秒数来なければ、切れたとみなす
GAP_NOTE_S = 3.0           # 知らせがこの秒数より長く止まった後にまた届いたら、ログのファイルに残す
BOOT_ALIVE_TIMEOUT_S = 30.0  # 初期化中(画面が「初期化中」)の上限。知らせは 2〜3 秒おきに延び、初期化の終わりに約 7 秒止まって、
                             # 同じ接続でまた届く(2026-11-13 実機 2 回: 7.5 / 7.0 秒)。USB を抜いたときは Windows がすぐ切るので待たない
IN_USE = True              # 本体画面で「接続」した接続か(GUI は False で始め、「接続」で True・切れたら False。測定の再開はこれを待つ)
AUTO_CONNECT = True        # shared_link: つながっていなければつなぐか。GUI は False(つなぐのは本体画面の「接続」だけ。
                           # 2026-11-12: 状態の読み取りも別につなぎ直していて、機器にとって相手が入れ替わり続け、つないでは切れるを繰り返した)
REFUSED_WAIT_S, REFUSED_RETRY_S = 90.0, 2.0   # 拒否されたら(初期化の途中)、この秒数まで、この間隔で試し直す
CHECK_PRESENT = True       # shared_link: つなぐ前に機器がいるか確かめ、いなければすぐ「見つかりません」(USB が無いと 10 秒×2 待っていた)   # つなぐ回数と間隔(2026-11-12 実機: 起動直後は1回目に画面が届かず、2回目でつながった)


def shared_link(connect: bool | None = None, host: str | None = None, source: str | None = None,
                refused_wait: float | None = None) -> DeviceLink:
    """アプリで共有する接続。つながっていなければつなぐ(切れていたらつなぎ直す)。失敗したら OSError。
    connect=False(GUI の状態の読み取り・測定。AUTO_CONNECT で決める)なら、つながっていなくてもつなぎに行かず OSError"""
    global LINK
    with _LINK_LOCK:
        if LINK is not None and host and (LINK.host, LINK.source) != (host, source):   # 別の機器(別の接続)を選んだ
            LINK.close()
            LINK = None
        if LINK is None:
            LINK = DeviceLink(host or HOST, source=source)
        if not LINK.connected and not (AUTO_CONNECT if connect is None else connect):
            raise ConnectionError("機器につながっていません")
        if not LINK.connected:
            if CHECK_PRESENT and not device_present(LINK.host, LINK.source):   # いなければすぐ返す(ping)
                raise ConnectionError("機器が見つかりません(電源・USB)")
            NOTE("[時間] ping が通った")
            end = time.time() + (REFUSED_WAIT_S if refused_wait is None else refused_wait)
            while True:
                try:
                    LINK.connect()
                    break
                except ConnectionRefusedError:
                    # 拒否 = 機器はまだ本体画面を送るしくみが動いていない(電源を入れた直後)か、前の接続を片付けている途中。
                    # 接続は残らないので、機器をふさがずに試し直せる。準備ができたらそのままつながる
                    NOTE("[時間] 55102 に断られた(試し直す)")
                    LINK.close(abort=True)
                    if time.time() > end:
                        raise
                    time.sleep(REFUSED_RETRY_S)
                except OSError:
                    LINK.close(abort=True)          # ほかの失敗はすぐにはつなぎ直さない(機器が前の接続の後始末を終えるまで答えないため)
                    raise
        return LINK


def device_present(host: str = HOST, source: str | None = None) -> bool:
    """機器が見つかるか: ping が届くか(機器の操作のしくみ(55100〜55102)には触れない)。
    USB が無い・電源が切れているときは PRESENT_TIMEOUT_MS で False(10 秒待たない)。
    2026-11-12 実機: 画面のポートにつないですぐ切って確かめると、機器がその後しばらく次の接続を拒否したり、
    つながっても画面を送ってこなかったりした(同時に1つの相手としか話さないため)。PC のアドレスから調べる方法は、
    USB がつながった直後に新しいアドレスが出てこないことがあった。どちらもやめて ping にした"""
    import subprocess
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", str(PRESENT_TIMEOUT_MS), *(["-S", source] if source else []), host],
                           capture_output=True,
                           timeout=PRESENT_TIMEOUT_MS / 1000 + 3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and b"TTL=" in r.stdout.upper()   # 「宛先に到達できません」も 0 を返すので、返事(TTL=)を見る


PRESENT_TIMEOUT_MS = 500   # device_present: ping の返事を待つ時間

# ---- 機器を探す: USB でつながった Bruker S1 Titan の接続だけを拾う(2026-11-12)
#   PC 側に「Bruker Remote NDIS Compatible Device」のネットワークの接続として現れる。USB の識別番号(VID 226B)と、
#   機器が名乗る名前(「Bruker S1 Titan - RNDIS」)で見分ける。差す口・ハブが変わると Windows は別の接続として登録し直すが
#   (イーサネット 4 / 5 …)、識別番号と名前は機器が名乗るので変わらない。前に使った口の登録も残るので、今有効な接続だけを使う。
#   機器のアドレスはその接続の DHCP サーバー(= ゲートウェイ)、PC 側のアドレスはその接続のもの(固定のアドレスは使わない)
USB_VENDOR = r"USB\VID_226B"                         # Bruker AXS Handheld の USB の識別番号
DEVICE_MATCH = "Bruker S1 Titan"                      # 機器が USB で名乗る名前の頭
_NET_CLASS = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"
# Windows から直接読む(PowerShell は使わない。2026-11-12 実機で測った: PowerShell だと 1回目 約 4 秒・2回目から 約 0.5 秒、
# 直接読むと 約 0.01 秒)。接続の一覧は GetAdaptersAddresses、USB の機器の名乗りは CM_Get_DevNode_PropertyW
class _SockAddr(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _Addr(ctypes.Structure):           # IP_ADAPTER_UNICAST_ADDRESS / IP_ADAPTER_GATEWAY_ADDRESS の頭
    pass


_Addr._fields_ = [("Length", ctypes.c_ulong), ("Flags", ctypes.c_ulong), ("Next", ctypes.POINTER(_Addr)),
                  ("Address", _SockAddr)]


class _Adapter(ctypes.Structure):        # IP_ADAPTER_ADDRESSES(使う所まで)
    pass


_Adapter._fields_ = [
    ("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong), ("Next", ctypes.POINTER(_Adapter)),
    ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(_Addr)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p), ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong), ("IfType", ctypes.c_ulong), ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", ctypes.c_ulong), ("ZoneIndices", ctypes.c_ulong * 16), ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong), ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p), ("FirstGatewayAddress", ctypes.POINTER(_Addr)),
    ("Ipv4Metric", ctypes.c_ulong), ("Ipv6Metric", ctypes.c_ulong), ("Luid", ctypes.c_ulonglong),
    ("Dhcpv4Server", _SockAddr)]


def _ipv4(sa: _SockAddr) -> str:
    if not sa.lpSockaddr or sa.iSockaddrLength < 8:
        return ""
    raw = ctypes.string_at(sa.lpSockaddr, sa.iSockaddrLength)
    return socket.inet_ntoa(raw[4:8]) if int.from_bytes(raw[:2], "little") == socket.AF_INET else ""


def _adapters() -> list[dict]:
    """今有効な(つながっている)ネットワークの接続: [{id: GUID, ip: PC 側のアドレス, host: DHCP サーバー(なければゲートウェイ)}]"""
    size = ctypes.c_ulong(16384)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        r = ctypes.windll.iphlpapi.GetAdaptersAddresses(socket.AF_INET, 0x80, None, buf, ctypes.byref(size))   # 0x80 = ゲートウェイも
        if r == 0:
            break
    else:
        return []
    # 1件目は from_buffer で読む(ctypes.cast は循環参照を作り、呼ぶたびにバッファが GC まで残る。2026-11-12 測定:
    # 1 秒おきに呼ぶと GC までに最大 約 17 MB 溜まった)。2件目からは Next(ポインター)をたどる
    out, a = [], _Adapter.from_buffer(buf)
    while a is not None:
        if a.OperStatus == 1 and a.AdapterName:      # 1 = つながっている
            out.append({"id": a.AdapterName.decode("ascii", "replace"),
                        "ip": _ipv4(a.FirstUnicastAddress.contents.Address) if a.FirstUnicastAddress else "",
                        "host": _ipv4(a.Dhcpv4Server) or (_ipv4(a.FirstGatewayAddress.contents.Address)
                                                          if a.FirstGatewayAddress else "")})
        a = a.Next.contents if a.Next else None
    return out


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", ctypes.c_ulong)]


def _bus_desc(pnp: str) -> str:
    """USB の機器が名乗る名前(DEVPKEY_Device_BusReportedDeviceDesc。例「Bruker S1 Titan - RNDIS」)。つながっていなければ空"""
    import uuid
    key = _DevPropKey()
    ctypes.memmove(key.fmtid, uuid.UUID("540b947e-8b40-45bc-a8a2-6a0b894cbda2").bytes_le, 16)
    key.pid = 4
    cm = ctypes.windll.cfgmgr32
    dev, ptype, size = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong(0)
    if cm.CM_Locate_DevNodeW(ctypes.byref(dev), ctypes.c_wchar_p(pnp), 0) != 0:
        return ""
    cm.CM_Get_DevNode_PropertyW(dev, ctypes.byref(key), ctypes.byref(ptype), None, ctypes.byref(size), 0)
    if not size.value:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if cm.CM_Get_DevNode_PropertyW(dev, ctypes.byref(key), ctypes.byref(ptype), buf, ctypes.byref(size), 0) != 0:
        return ""
    return buf.raw.decode("utf-16-le", "replace").rstrip("\0")


def find_s1_titan(pending: bool = False) -> list[dict]:
    """今有効な、USB でつながった Bruker S1 Titan の接続。[{host: 機器のアドレス, source: PC 側のアドレス, alias: 接続の名前}]。
    pending=True なら、アドレスがまだ無い接続も返す(host・source は空。USB が現れてから約 3 秒は無い)"""
    import winreg
    try:
        active = _adapters()
    except Exception:
        return []
    found = []
    for a in active:
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _NET_CLASS + "\\" + a["id"] + "\\Connection")
            pnp = winreg.QueryValueEx(k, "PnPInstanceId")[0]
            alias = winreg.QueryValueEx(k, "Name")[0]
        except (OSError, KeyError, TypeError):
            continue
        if not pnp.upper().startswith(USB_VENDOR) or not pending and not (a.get("host") and a.get("ip")):
            continue
        try:
            if not _bus_desc(pnp).startswith(DEVICE_MATCH):
                continue
        except Exception:
            continue
        found.append({"host": a.get("host") or "", "source": a.get("ip") or "", "alias": alias})
    return found


def current_link() -> DeviceLink | None:
    """つながっている接続(無ければ None。つなぎに行かない)"""
    return LINK if LINK is not None and LINK.connected else None


def close_link():
    if LINK is not None:
        LINK.close()


NEW_SCREENS_MAX = 40      # 見本に無かった画面を残す上限(同じ画面は1枚)


def _keep_new_screen(raw: Image.Image):
    """見本に無かった画面を残す(照合の見本を足すため)。同じ画面は1枚、上限を超えたら残さない"""
    try:
        import hashlib
        from storage import NEW_SCREENS_DIR
        os.makedirs(NEW_SCREENS_DIR, exist_ok=True)
        body = raw.crop((0, screen_match.STATUS_H, raw.width, raw.height)).tobytes()   # 上の状態の行(時刻など)は除いて比べる
        name = hashlib.md5(body).hexdigest()[:12] + ".png"
        have = os.listdir(NEW_SCREENS_DIR)
        if name not in have and len(have) < NEW_SCREENS_MAX:
            raw.save(os.path.join(NEW_SCREENS_DIR, name))
    except OSError:
        pass


CLICK_WAIT_S = 1.5        # クリックの後、画面の更新を待つ上限(何も変わらないクリックならここで諦める)
SETTLE_S = 0.25           # 更新が届き始めてから、続きを待つ時間


class DirectRemote:
    """bruker_ui.RemoteCtrl と同じ使い方で、直接つないだ機器を操作する(auto_measure.Operator から使う)。
    画像は 200% 表示の RemoteCTRL と同じ形(機器の画面を縦横2倍、最近傍)にして、今の画面解析をそのまま使う"""

    SCALE = 2

    def __init__(self, link: DeviceLink | None = None):
        self.link = link or shared_link()
        self.serial = self.link.name                # つなぎ直したとき、同じ機器か確かめる(relink)
        self._clicked_at: int | None = None         # クリックした時点の更新の回数(その後の更新を待つため)

    def relink(self) -> bool:
        """つなぎ直した接続に付け替える(測定の再開)。本体画面で「接続」した、前と同じ機器(製造番号)で、画面とトリガーの状態の
        接続がそろっていれば True(自動で確かめている途中の接続ではまだ再開しない)"""
        link = current_link()
        if link is None or not IN_USE or "status" not in link._socks or (self.serial and link.name != self.serial):
            return False
        self.link, self._clicked_at = link, None
        return True

    def content_width(self) -> int:
        return self.link.width * self.SCALE

    def capture_background(self) -> Image.Image:
        """最新の画面。クリックの後だけ、画面の更新が届くまで待つ(変わらない画面では待たない)"""
        if self.link.error:
            raise ConnectionError(f"機器との接続が切れました: {self.link.error}")
        if self.link.seq == 0 and not self.link.wait_frame(0, FRAME_WAIT_S):   # つないだ直後で、まだ画面が来ていない
            raise ConnectionError("機器の画面が届きません")
        if self._clicked_at is not None:
            if self.link.wait_frame(self._clicked_at, CLICK_WAIT_S):
                time.sleep(SETTLE_S)                    # 更新は何回かに分かれて届くので、続きを少し待つ
            self._clicked_at = None
        img = self.link.snapshot()
        return img.resize((img.width * self.SCALE, img.height * self.SCALE), Image.NEAREST)

    def read_ui(self, img: Image.Image | None = None, content_w: int | None = None) -> ScreenUI:
        """画面を見本との照合で読む(screen_match。OCR は使わない)。見本で読めない画面は、そのまま返し
        (状態は unknown になり、測定の処理は先へ進まずに止まる)、見本を足すために残す(NEW_SCREENS_DIR、最大 NEW_SCREENS_MAX 枚)"""
        img = img or self.capture_background()
        raw = img.resize((img.width // self.SCALE, img.height // self.SCALE), Image.NEAREST)   # 2倍にした絵を元に戻す
        ui = screen_match.library().read(raw)
        if not screen_match.recognized(ui):
            _keep_new_screen(raw)
        return ui

    def to_client(self, ui: ScreenUI, pt: tuple[int, int]) -> tuple[int, int]:
        """基準座標(REF_W 幅)→ 機器の画面の座標"""
        k = self.link.width / REF_W
        return round(pt[0] * k), round(pt[1] * k)

    def click(self, x: int, y: int):
        self._clicked_at = self.link.seq
        self.link.click(x, y)

    def trigger_state(self) -> str:
        """'pull'(照射できる)/ 'release'(照射中)/ 'unknown'(ログイン画面などで照射できない・不明)。
        つないでからまだ知らせが来ていなければ、STATUS_WAIT_S まで待つ(すぐ測定を始めても「不明」で止まらないように)"""
        if self.link.status is None:
            self.link.wait_status(tuple(STATUS.values()), STATUS_WAIT_S)
        return {"released": "pull", "pulled": "release"}.get(self.link.status, "unknown")

    def trigger(self):
        """RemoteCTRL のメニュー(F6)と同じく、照射中なら止め、そうでなければ照射を始める"""
        if self.link.status == "pulled":
            self.link.release()
        else:
            self.link.pull()

    def release(self) -> bool:
        """照射を止めるだけ(照射中でなければ何もしない。trigger と違い、照射を始める方へは切り替えない)"""
        return self.link.release()
