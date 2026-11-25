"""蓋の磁気センサー(USB 413d:2107。スイッチ 2 = F24 に書き換え済み。LID_SENSOR.md)を読む(画面を持たない。センサーへは何も送らない)。

磁石が近い(蓋を閉じた)間、センサーは F24 を押したままにする。Windows がその間の連打(約 30 ms おき)を作る。
専用の糸が見えない窓で Raw Input を受け、送り元が VID_413D の F24 だけを数える(アプリの窓が選ばれていなくても分かる)。
連打 1 回ごとの仕事は時刻を書き換えるだけで、知らせる(add_listener)のは状態が切り替わったときだけ。

状態: "closed"(蓋が閉じている = 照射してよい)/ "open"(開いている)/ "none"(センサーが見つからない・読めない)。
閉になるのは F24 が押されている間だけ。離した知らせ・連打の途絶え(Windows のキーの設定から求めた時間)・USB が無いときは
閉にしない(照射しない側に倒す)。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as W
import threading
import time

VID = "VID_413D"
VK_F24 = 0x87
PRESENT_S = 1.0          # USB にセンサーがあるかを確かめる間隔
TICK_MS = 100            # 連打の途絶えを見る間隔
MARGIN_S = 0.3           # 連打の間隔に足すゆとり

CLOSED, OPEN, NONE = "closed", "open", "none"


def repeat_gap_s() -> float:
    """押したままのキーの知らせの、いちばん長い間(最初の連打までの時間と連打の間隔の長い方。Windows のキーボードの設定から)"""
    try:
        u32 = ctypes.windll.user32
        delay, speed = W.UINT(1), W.UINT(31)
        u32.SystemParametersInfoW(0x16, 0, ctypes.byref(delay), 0)    # SPI_GETKEYBOARDDELAY: 0〜3 → 250〜1000 ms
        u32.SystemParametersInfoW(0x0A, 0, ctypes.byref(speed), 0)    # SPI_GETKEYBOARDSPEED: 0〜31 → 約 2.5〜30 回/秒
        first = (delay.value + 1) * 0.25
        every = 1 / (2.5 + speed.value * 27.5 / 31)
        return max(first, every)
    except Exception:
        return 1.0


class _Listeners:
    def __init__(self):
        self._fns = []
        self.state = NONE

    def add_listener(self, fn):
        """fn(状態) を状態が切り替わったときに呼ぶ(センサーの糸から。画面へは各画面の決まりで渡す)"""
        self._fns.append(fn)

    def closed(self) -> bool:
        return self.state == CLOSED

    def _set(self, new: str):
        if new == self.state:
            return
        self.state = new
        for fn in list(self._fns):
            try:
                fn(new)
            except Exception:
                pass


class Fixed(_Listeners):
    """テスト・デモ用の偽のセンサー(set で状態を変える)"""

    def __init__(self, state: str = CLOSED):
        super().__init__()
        self.state = state

    def set(self, state: str):
        self._set(state)


# ---- Raw Input
WM_INPUT, WM_TIMER = 0x00FF, 0x0113
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x100
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
                ("lpszClassName", W.LPCWSTR)]


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", W.USHORT), ("usUsage", W.USHORT), ("dwFlags", W.DWORD), ("hwndTarget", W.HWND)]


class _RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", W.HANDLE), ("dwType", W.DWORD)]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD), ("hDevice", W.HANDLE), ("wParam", W.WPARAM)]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", W.USHORT), ("Flags", W.USHORT), ("Reserved", W.USHORT),
                ("VKey", W.USHORT), ("Message", W.UINT), ("ExtraInformation", W.ULONG)]


class LidSensor(_Listeners):
    """本物のセンサー。作ると専用の糸が動き出す"""

    def __init__(self):
        super().__init__()
        self.gap_s = repeat_gap_s() + MARGIN_S
        self._held = False
        self._last = 0.0
        self._present = False
        self._checked = 0.0
        self._names: dict[int, str] = {}
        self.error = ""
        threading.Thread(target=self._run, name="lid_sensor", daemon=True).start()

    def _name(self, u32, h) -> str:
        key = h or 0
        if key not in self._names:
            n = W.UINT(0)
            u32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, None, ctypes.byref(n))
            buf = ctypes.create_unicode_buffer(n.value + 1)
            u32.GetRawInputDeviceInfoW(h, RIDI_DEVICENAME, buf, ctypes.byref(n))
            self._names[key] = buf.value.upper()
        return self._names[key]

    def _find(self, u32) -> bool:
        """USB にセンサー(のキーボードの口)があるか"""
        n = W.UINT(0)
        size = ctypes.sizeof(_RAWINPUTDEVICELIST)
        if u32.GetRawInputDeviceList(None, ctypes.byref(n), size) != 0:
            return False
        arr = (_RAWINPUTDEVICELIST * (n.value + 4))()
        n.value += 4
        got = u32.GetRawInputDeviceList(arr, ctypes.byref(n), size)
        if got == ctypes.c_uint(-1).value:
            return False
        self._names = {k: v for k, v in self._names.items() if k in {d.hDevice or 0 for d in arr[:got]}}
        return any(d.dwType == RIM_TYPEKEYBOARD and VID in self._name(u32, d.hDevice) for d in arr[:got])

    def _eval(self, u32, now: float):
        if now - self._checked >= PRESENT_S:
            self._checked = now
            self._present = self._find(u32)
        if self._held and now - self._last > self.gap_s:
            self._held = False                       # 連打が途絶えた(離した知らせが来なかった): 閉にしない
        self._set(NONE if not self._present else CLOSED if self._held else OPEN)

    def _run(self):
        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            u32.DefWindowProcW.restype = LRESULT
            u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
            u32.GetRawInputData.argtypes = [W.HANDLE, W.UINT, ctypes.c_void_p, ctypes.POINTER(W.UINT), W.UINT]
            u32.GetRawInputDeviceInfoW.argtypes = [W.HANDLE, W.UINT, ctypes.c_void_p, ctypes.POINTER(W.UINT)]
            u32.GetRawInputDeviceList.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.UINT), W.UINT]
            u32.GetRawInputDeviceList.restype = W.UINT
            u32.CreateWindowExW.restype = W.HWND
            u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
            u32.SetTimer.argtypes = [W.HWND, ctypes.c_size_t, W.UINT, ctypes.c_void_p]
            hdr = ctypes.sizeof(_RAWINPUTHEADER)

            def wndproc(hwnd, msg, wp, lp):
                if msg == WM_INPUT:
                    size = W.UINT(0)
                    u32.GetRawInputData(lp, RID_INPUT, None, ctypes.byref(size), hdr)
                    buf = ctypes.create_string_buffer(size.value)
                    u32.GetRawInputData(lp, RID_INPUT, buf, ctypes.byref(size), hdr)
                    h = _RAWINPUTHEADER.from_buffer_copy(buf, 0)
                    if h.dwType == RIM_TYPEKEYBOARD:
                        kb = _RAWKEYBOARD.from_buffer_copy(buf.raw[hdr:])
                        if kb.VKey == VK_F24 and VID in self._name(u32, h.hDevice):
                            now = time.monotonic()
                            self._present = True
                            if kb.Flags & 1:             # 離した(磁石が離れた)
                                self._held = False
                            else:
                                self._held, self._last = True, now
                            self._eval(u32, now)
                    return u32.DefWindowProcW(hwnd, msg, wp, lp)
                if msg == WM_TIMER:
                    self._eval(u32, time.monotonic())
                    return 0
                return u32.DefWindowProcW(hwnd, msg, wp, lp)

            self._proc = WNDPROC(wndproc)                  # 糸が生きている間は持っておく
            hinst = k32.GetModuleHandleW(None)
            wc = _WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="Traer5iLidSensor")
            u32.RegisterClassW(ctypes.byref(wc))
            hwnd = u32.CreateWindowExW(0, "Traer5iLidSensor", "", 0, 0, 0, 0, 0, W.HWND(-3), None, hinst, None)
            dev = _RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)
            if not hwnd or not u32.RegisterRawInputDevices(ctypes.byref(dev), 1, ctypes.sizeof(_RAWINPUTDEVICE)):
                self.error = f"Raw Input を登録できません(err={ctypes.get_last_error()})"
                return
            u32.SetTimer(hwnd, 1, TICK_MS, None)
            self._eval(u32, time.monotonic())
            msg = W.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:                             # 読めない: 状態は「なし」のまま(照射しない)
            self.error = str(e)
            self._set(NONE)


_SENSOR: _Listeners | None = None


def start() -> _Listeners:
    """アプリで 1 つのセンサー(はじめて呼んだときに糸が動き出す)。テストはこれを差し替える"""
    global _SENSOR
    if _SENSOR is None:
        _SENSOR = LidSensor()
    return _SENSOR
