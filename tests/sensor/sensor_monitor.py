"""磁気センサー(USB 413d:2107)の入力のオン / オフを見る簡単な窓。センサーへは何も送らない。

2 つの方法で見る:
  1. Raw Input: 送り元が VID_413D のキー入力だけを受ける(どの機器から来たか分かる)
  2. F24 の押し下げ: GetAsyncKeyState(VK_F24)。どの機器からでも、窓が選ばれていなくても分かる
センサーは片方の状態の間、キーを押したままにする(Windows のキーの連打が続く)。
キーが離された / 連打が OFF_AFTER_S 途絶えたら オフ とする。

使い方(TRACERexl で): python tests/sensor/sensor_monitor.py
"""
import ctypes, ctypes.wintypes as W, queue, threading, time
import tkinter as tk
from tkinter import ttk

TARGET = "VID_413D"
VK_F24 = 0x87
OFF_AFTER_S = 0.6     # 連打がこれだけ途絶えたらオフ(連打は約 30 ms おき。最初の連打までは約 0.5 秒)
POLL_MS = 30

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
WM_INPUT = 0x00FF
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x100
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                ("lpszClassName", W.LPCWSTR)]
class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", W.USHORT), ("usUsage", W.USHORT), ("dwFlags", W.DWORD), ("hwndTarget", W.HWND)]
class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD), ("hDevice", W.HANDLE), ("wParam", W.WPARAM)]
class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", W.USHORT), ("Flags", W.USHORT), ("Reserved", W.USHORT),
                ("VKey", W.USHORT), ("Message", W.UINT), ("ExtraInformation", W.ULONG)]

u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
u32.GetRawInputData.argtypes = [W.HANDLE, W.UINT, ctypes.c_void_p, ctypes.POINTER(W.UINT), W.UINT]
u32.GetRawInputDeviceInfoW.argtypes = [W.HANDLE, W.UINT, ctypes.c_void_p, ctypes.POINTER(W.UINT)]
u32.CreateWindowExW.restype = W.HWND
u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
u32.GetAsyncKeyState.restype = ctypes.c_short


def raw_input_thread(q):
    """キー入力を受ける見えない窓を持つ糸。(VKey, 離したか, 送り元がセンサーか, 機器名) を q へ。"""
    names = {}
    def dev_name(h):
        key = h or 0
        if key not in names:
            n = W.UINT(0)
            u32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, ctypes.byref(n))
            buf = ctypes.create_unicode_buffer(n.value + 1)
            u32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, buf, ctypes.byref(n))
            names[key] = buf.value
        return names[key]

    def wndproc(hwnd, msg, wp, lp):
        if msg == WM_INPUT:
            size = W.UINT(0); hdr = ctypes.sizeof(RAWINPUTHEADER)
            u32.GetRawInputData(lp, RID_INPUT, None, ctypes.byref(size), hdr)
            buf = ctypes.create_string_buffer(size.value)
            u32.GetRawInputData(lp, RID_INPUT, buf, ctypes.byref(size), hdr)
            h = RAWINPUTHEADER.from_buffer_copy(buf, 0)
            if h.dwType == RIM_TYPEKEYBOARD:
                kb = RAWKEYBOARD.from_buffer_copy(buf.raw[hdr:])
                name = dev_name(h.hDevice)
                is_sensor = TARGET in name.upper()
                # センサー以外のキーは、F24 のときだけ知らせる(ふつうの打鍵の中身は扱わない)
                if is_sensor or kb.VKey == VK_F24:
                    q.put((kb.VKey, bool(kb.Flags & 1), is_sensor, name))
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    proc = WNDPROC(wndproc)
    hinst = k32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName="SensorMonitorRaw")
    u32.RegisterClassW(ctypes.byref(wc))
    hwnd = u32.CreateWindowExW(0, "SensorMonitorRaw", "", 0, 0, 0, 0, 0, W.HWND(-3), None, hinst, None)
    dev = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)
    if not u32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(RAWINPUTDEVICE)):
        q.put(("error", ctypes.get_last_error()))
        return
    msg = W.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.DispatchMessageW(ctypes.byref(msg))


KEYNAME = {VK_F24: "F24", **{0x41 + i: chr(0x41 + i) for i in range(26)}}


class Monitor:
    COLORS = {True: ("#d9f2e3", "#11703a", "● オン"), False: ("#eeeeee", "#555555", "○ オフ")}

    def __init__(self, root):
        self.root = root
        root.title("磁気センサーの確認")
        root.geometry("420x480")
        self.q = queue.Queue()
        threading.Thread(target=raw_input_thread, args=(self.q,), daemon=True).start()
        self.t0 = time.time()
        self.last_sensor = 0.0       # センサーから最後に押し下げが来た時刻
        self.sensor_on = False
        self.f24_on = False
        self.changes = 0
        self.events = 0

        pad = {"padx": 12, "pady": 6}
        ttk.Label(root, text="センサー(VID_413D)からのキー入力").pack(anchor="w", **pad)
        self.big = tk.Label(root, font=("Yu Gothic UI", 28, "bold"), width=12, height=2)
        self.big.pack(fill="x", padx=12)
        self.detail = ttk.Label(root, text="")
        self.detail.pack(anchor="w", **pad)

        ttk.Label(root, text="F24 キーの押し下げ(どの機器からでも)").pack(anchor="w", **pad)
        self.small = tk.Label(root, font=("Yu Gothic UI", 14, "bold"), height=1)
        self.small.pack(fill="x", padx=12)

        ttk.Label(root, text="変化の記録").pack(anchor="w", **pad)
        self.log = tk.Listbox(root, height=10)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.show()
        root.after(POLL_MS, self.tick)

    def note(self, text):
        self.log.insert(0, f"{time.strftime('%H:%M:%S')}  {text}")
        if self.log.size() > 200:
            self.log.delete(200, "end")

    def tick(self):
        now = time.time()
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "error":
                self.note(f"Raw Input を登録できない err={item[1]}")
                continue
            vk, up, is_sensor, name = item
            if not is_sensor:
                self.note(f"F24 が別の機器から来た: {name[:60]}")
                continue
            self.events += 1
            self.last_key = KEYNAME.get(vk, f"0x{vk:02X}")
            if up:
                self.set_sensor(False, f"{self.last_key} を離した")
            else:
                self.last_sensor = now
                self.set_sensor(True, f"{self.last_key} を押した")
        if self.sensor_on and now - self.last_sensor > OFF_AFTER_S:
            self.set_sensor(False, "連打が途絶えた")
        f24 = bool(u32.GetAsyncKeyState(VK_F24) & 0x8000)
        if f24 != self.f24_on:
            self.f24_on = f24
            self.note(f"F24 の押し下げ: {'あり' if f24 else 'なし'}")
        self.show()
        self.root.after(POLL_MS, self.tick)

    def set_sensor(self, on, why):
        if on != self.sensor_on:
            self.sensor_on = on
            self.changes += 1
            self.note(f"センサー {'オン' if on else 'オフ'}({why})")

    def show(self):
        bg, fg, text = self.COLORS[self.sensor_on]
        self.big.config(bg=bg, fg=fg, text=text)
        bg2, fg2, text2 = self.COLORS[self.f24_on]
        self.small.config(bg=bg2, fg=fg2, text=text2)
        key = getattr(self, "last_key", "—")
        self.detail.config(text=f"最後のキー: {key}   受けた入力: {self.events} 件   切り替わり: {self.changes} 回")


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Monitor(root)
    root.mainloop()
