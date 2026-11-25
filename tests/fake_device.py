"""テスト用: 機器の偽物(PC の中の小さなサーバー)。PROTOCOL_55100.md の形で、画面・トリガーの状態・命令の返事を返す。
本物の機器には触れない。FakeDevice().start() で空いている番号を3つ使って待ち受ける(ports で番号がわかる)"""
from __future__ import annotations

import socket, struct, threading, time

W, H = 480, 640
WHITE, BLUE, RED = 0xFFFF, 0x001F, 0xF800
BUTTON = (240, 100, 400, 200)              # 青いボタン(機器の画面の座標)


def rle(rect, color_at) -> bytes:
    """範囲 rect を (個数, 色) の並びにする"""
    l, t, r, b = rect
    out, run, cur = [], 0, None
    for y in range(t, b):
        for x in range(l, r):
            c = color_at(x, y)
            if c == cur and run < 0xFFFF:
                run += 1
            else:
                if cur is not None:
                    out.append(struct.pack("<HH", run, cur))
                cur, run = c, 1
    out.append(struct.pack("<HH", run, cur))
    return b"".join(out)


def update(rect, color_at) -> bytes:
    data = rle(rect, color_at)
    return struct.pack("<6I", 0x14, len(data) // 2, *rect) + data


class FakeDevice:
    def __init__(self, status="TRIGGER_RELEASED", answer_ok=True, screen=None, send_screen=True):
        self.screen = screen                               # 最初に送る画面(PIL の 480x640。無ければ白に青いボタン)
        self.send_screen = send_screen                     # False: つないでも最初の画面を送らない(起動の途中などのまね。show で送る)
        self.clicks: list[tuple[int, int, int]] = []     # (種類, x, y)
        self.commands: list[str] = []
        self.hello = b""
        self.status, self.answer_ok = status, answer_ok
        self.socks: dict[str, socket.socket] = {}
        self.reaccept: dict[str, bool] = {}               # cut で切った接続を、またつながれるまで待つか
        self.silent = False                                # True: 生存の知らせを止める(通信が黙って途絶えたことにする)
        self._send_lock = threading.Lock()                 # 画面の接続へ送るものが混ざらないように
        self._listen = {k: socket.socket() for k in ("screen", "status", "command")}
        for s in self._listen.values():
            s.bind(("127.0.0.1", 0)); s.listen(2)
        self.ports = tuple(self._listen[k].getsockname()[1] for k in ("screen", "status", "command"))

    def start(self):
        for k in self._listen:
            threading.Thread(target=self._serve, args=(k,), daemon=True).start()
        return self

    def _serve(self, k):
        try:
            c, _ = self._listen[k].accept()
        except OSError:                                    # つながれないまま閉じた(drop)
            return
        self.socks[k] = c
        try:
            if k == "screen":
                info = struct.pack("<I4s5I", 316, b"0mmj", W, H, 16, 4, 8).ljust(36, b"\0") + b"EVM3730\0LOGIC\0FAKE001\0"
                c.sendall(info.ljust(332, b"\0"))
                self.hello = c.recv(4)
                if not self.send_screen:
                    pass
                elif self.screen is not None:
                    px = self.screen.convert("RGB").load()
                    to565 = lambda p: ((p[0] >> 3) << 11) | ((p[1] >> 2) << 5) | (p[2] >> 3)
                    c.sendall(update((0, 0, W, H), lambda x, y: to565(px[x, y])))
                else:
                    c.sendall(update((0, 0, W, H), lambda x, y: BLUE if BUTTON[0] <= x < BUTTON[2] and BUTTON[1] <= y < BUTTON[3] else WHITE))
                self._send(c, struct.pack("<I", 0x19))
                threading.Thread(target=self._alive, args=(c,), daemon=True).start()
                while True:
                    ev = c.recv(6)
                    if len(ev) < 6:
                        break
                    t, x, y = struct.unpack("<3H", ev)
                    self.clicks.append((t, x, y))
                    if t == 4:                               # 離したら、押した所に 8x8 の赤い印を描く
                        self._send(c, update((x, y, x + 8, y + 8), lambda *_: RED))
            elif k == "status":
                time.sleep(0.2)
                self.say(self.status)
                try:
                    while c.recv(64):
                        pass
                except OSError:                            # cut で閉じた
                    pass
                if self.reaccept.get(k):               # 機器が切った後、またつながれるのを待つ(トリガーの接続のつなぎ直し)
                    self.reaccept[k] = False
                    return self._serve(k)
            else:
                buf = b""
                while True:
                    d = c.recv(64)
                    if not d:
                        break
                    buf += d
                    while b"\r" in buf:
                        line, buf = buf.split(b"\r", 1)
                        cmd = line.decode()
                        self.commands.append(cmd)
                        if self.answer_ok:
                            c.sendall(b"OK\r")
                        if cmd == "TRIGGER_PULL":
                            self.say("TRIGGER_PULLED")
                        elif cmd == "TRIGGER_RELEASE":
                            self.say("TRIGGER_NOT_READY"); self.say("TRIGGER_RELEASED")
        except OSError:
            pass

    def show(self, screen):
        """機器の画面を screen(PIL の 480x640)に変える(デモ用)。つながっていなければ何もしない"""
        c = self.socks.get("screen")
        if c is None:
            return
        px = screen.convert("RGB").load()
        to565 = lambda p: ((p[0] >> 3) << 11) | ((p[1] >> 2) << 5) | (p[2] >> 3)
        try:
            self._send(c, update((0, 0, W, H), lambda x, y: to565(px[x, y])))
        except OSError:
            pass

    def _send(self, c, data: bytes):
        with self._send_lock:
            c.sendall(data)

    def _alive(self, c):
        """生存の知らせ(本物は約 1.1 秒ごと)"""
        try:
            while True:
                time.sleep(1.0)
                if not self.silent:
                    self._send(c, struct.pack("<I", 0x19))
        except OSError:
            pass

    def say(self, status: str):
        self.status = status
        for _ in range(50):
            if "status" in self.socks:
                break
            time.sleep(0.02)
        self.socks["status"].sendall(status.encode() + b"\r")

    def cut(self, key: str):
        """機器の側から1つの接続だけを切る(電源を入れた直後に、機器がトリガーの状態の接続を切ったことのまね)。
        また つながれたら受け付ける"""
        self.reaccept[key] = True
        c = self.socks.pop(key, None)
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def drop(self):
        """機器が切れたことにする"""
        for s in list(self.socks.values()) + list(self._listen.values()):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
