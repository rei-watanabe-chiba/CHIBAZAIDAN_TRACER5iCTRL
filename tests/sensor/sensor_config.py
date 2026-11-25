"""磁気センサー(USB 413d:2107。キーボードのふりをするスイッチ)の割り当ての読み出し・書き換え。

通信の形は オープンソースの footswitch(rgerganov/footswitch、src/footswitch.c)と同じ:
  読み出し: 01 82 08 N 00 00 00 00 を送り、8 バイト受ける(N = スイッチ 1〜3)
  書き換え: 開始 01 80 08 00 … → 1 秒待つ → スイッチごとに 頭 01 81 08 N 00 00 00 00 と
           中身 08 種類 修飾 キー 00 00 00 00(種類 1 = キー)。3 つとも必ず送る(30 ms おき)
Windows では報告の番号 0 を先頭に付けて 9 バイトで送る。

使い方(TRACERexl で):
  python tests/sensor/sensor_config.py               今の割り当てを読むだけ
  python tests/sensor/sensor_config.py --plan        書き換えで送る内容を出すだけ(送らない)
  python tests/sensor/sensor_config.py --write       書き換える(スイッチ 2 = F24。1 = a・3 = c はそのまま)→ 読み直す
  python tests/sensor/sensor_config.py --restore     元に戻す(a / b / c)→ 読み直す
書き換えは センサーの記憶に残る(恒久的)。
"""
import ctypes, ctypes.wintypes as W, sys, threading, time

# 書き換え後の割り当て: スイッチ番号 → (修飾, キーの HID コード)
TARGET = {1: (0, 0x04), 2: (0, 0x73), 3: (0, 0x06)}    # a / F24 / c
ORIGINAL = {1: (0, 0x04), 2: (0, 0x05), 3: (0, 0x06)}  # a / b / c(2026-11-20 に読み出した値)

hid = ctypes.WinDLL("hid"); sa = ctypes.WinDLL("setupapi"); k = ctypes.WinDLL("kernel32", use_last_error=True)

class GUID(ctypes.Structure): _fields_ = [("a", W.DWORD), ("b", W.WORD), ("c", W.WORD), ("d", ctypes.c_ubyte * 8)]
class DID(ctypes.Structure): _fields_ = [("cb", W.DWORD), ("g", GUID), ("f", W.DWORD), ("r", ctypes.c_size_t)]
sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
sa.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), W.DWORD, ctypes.POINTER(DID)]
sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DID), ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p]
sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
k.CreateFileW.restype = ctypes.c_void_p
k.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p]
k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p]
k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p]
k.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
k.CloseHandle.argtypes = [ctypes.c_void_p]

KEYS = {**{0x04 + i: chr(ord("a") + i) for i in range(26)}, **{0x1E + i: str((i + 1) % 10) for i in range(10)},
        0x28: "enter", 0x29: "esc", 0x2A: "backspace", 0x2B: "tab", 0x2C: "space",
        **{0x3A + i: f"f{i + 1}" for i in range(12)}, **{0x68 + i: f"f{i + 13}" for i in range(12)}}
MODS = ["l_ctrl", "l_shift", "l_alt", "l_win", "r_ctrl", "r_shift", "r_alt", "r_win"]


def find_path():
    """設定用の口(インターフェース 1 = MI_01)のパス。"""
    g = GUID(); hid.HidD_GetHidGuid(ctypes.byref(g))
    hs = sa.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)
    try:
        i = 0
        while True:
            d = DID(); d.cb = ctypes.sizeof(DID)
            if not sa.SetupDiEnumDeviceInterfaces(hs, None, ctypes.byref(g), i, ctypes.byref(d)):
                return None
            i += 1
            n = W.DWORD(); sa.SetupDiGetDeviceInterfaceDetailW(hs, ctypes.byref(d), None, 0, ctypes.byref(n), None)
            buf = ctypes.create_string_buffer(n.value); ctypes.cast(buf, ctypes.POINTER(W.DWORD))[0] = 8
            sa.SetupDiGetDeviceInterfaceDetailW(hs, ctypes.byref(d), buf, n, None, None)
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if "vid_413d&pid_2107&mi_01" in path.lower():
                return path
    finally:
        sa.SetupDiDestroyDeviceInfoList(hs)


def describe(r):
    t = r[1]
    if t == 0:
        return "割り当てなし"
    parts = []
    if t in (1, 0x81, 3):
        mods = [MODS[b] for b in range(8) if r[2] >> b & 1]
        key = KEYS.get(r[3], f"0x{r[3]:02X}") if r[3] else ""
        parts.append("キー " + "+".join(mods + ([key] if key else [])))
    if t in (2, 3):
        btn = [n for b, n in ((1, "左"), (2, "右"), (4, "中"), (8, "ダブル")) if r[4] & b]
        s = lambda v: v - 256 if v > 127 else v
        parts.append(f"マウス ボタン={'・'.join(btn) or 'なし'} X={s(r[5])} Y={s(r[6])} W={s(r[7])}")
    if t == 4:
        parts.append(f"文字列(長さ {r[0] - 2})")
    return " / ".join(parts) or f"不明な種類 0x{t:02X}"


def packets(assign):
    """書き換えで送る 8 バイトの並び(footswitch の write_pedals と同じ順)。"""
    out = [("開始", bytes([0x01, 0x80, 0x08, 0, 0, 0, 0, 0]))]
    for n in (1, 2, 3):
        mod, key = assign[n]
        out.append((f"スイッチ {n} 頭", bytes([0x01, 0x81, 0x08, n, 0, 0, 0, 0])))
        out.append((f"スイッチ {n} 中身", bytes([0x08, 0x01, mod, key, 0, 0, 0, 0])))
    return out


class Sensor:
    def __init__(self):
        path = find_path()
        if not path:
            raise SystemExit("センサーの設定用の口(MI_01)が見つからない。USB を確かめてください")
        self.h = k.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
        if not self.h or self.h == ctypes.c_void_p(-1).value:
            raise SystemExit(f"開けない err={ctypes.get_last_error()}")

    def close(self):
        k.CloseHandle(self.h)

    def send(self, data8):
        q = b"\x00" + data8  # 先頭は報告の番号 0
        w = W.DWORD()
        if not k.WriteFile(self.h, q, len(q), ctypes.byref(w), None):
            raise SystemExit(f"送れない err={ctypes.get_last_error()}")
        time.sleep(0.03)

    def recv(self, timeout=2.0):
        out = {}
        def run():
            buf = ctypes.create_string_buffer(9); n = W.DWORD()
            if k.ReadFile(self.h, buf, 9, ctypes.byref(n), None):
                out["data"] = buf.raw[1:n.value]
        t = threading.Thread(target=run, daemon=True); t.start(); t.join(timeout)
        if t.is_alive():
            k.CancelIoEx(self.h, None); t.join(1)
        return out.get("data")

    def read(self):
        res = {}
        for n in (1, 2, 3):
            self.send(bytes([0x01, 0x82, 0x08, n, 0, 0, 0, 0]))
            r = self.recv()
            if r is None:
                print(f"[スイッチ {n}] 返事が来ない"); break
            res[n] = r
            print(f"[スイッチ {n}] {r.hex(' ')}  → {describe(r)}")
        return res

    def write(self, assign):
        for i, (name, p) in enumerate(packets(assign)):
            self.send(p)
            if i == 0:
                time.sleep(1.0)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--plan":
        for name, p in packets(TARGET):
            print(f"{name:10s} {p.hex(' ')}")
        print("(送っていない)")
        return
    s = Sensor()
    try:
        if arg in ("--write", "--restore"):
            assign = TARGET if arg == "--write" else ORIGINAL
            print("書き換え前:"); s.read()
            s.write(assign)
            print("書き換えた。読み直す:")
            time.sleep(0.5)
            got = s.read()
            ok = all(got.get(n, b"\0" * 8)[1] in (1, 0x81) and got[n][2:4] == bytes(assign[n]) for n in (1, 2, 3))
            print("結果:", "思ったとおり" if ok else "思った値と違う")
        elif arg == "":
            s.read()
        else:
            print(__doc__)
    finally:
        s.close()


if __name__ == "__main__":
    main()
