"""センサーの F24 の連打が、Windows の「最後の操作の時刻」(GetLastInputInfo)を更新し続けるかを測る。
更新し続けるなら、蓋を閉じている間は PC がスリープ・自動ロックに入らない。センサーへは何も送らない。

使い方(TRACERexl で): python tests/sensor/idle_check.py [秒数(既定 60)]
測っている間はキーボード・マウスに触らない。磁石を近づけた状態(オン)と離した状態(オフ)を両方つくる。
"""
import ctypes, ctypes.wintypes as W, sys, time

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("dwTime", W.DWORD)]

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
u32.GetAsyncKeyState.restype = ctypes.c_short
k32.GetTickCount.restype = W.DWORD
VK_F24 = 0x87


def idle_s():
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    u32.GetLastInputInfo(ctypes.byref(li))
    return ((k32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000


def main():
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    t0 = time.time()
    rows = {True: [], False: []}   # F24 の押し下げ → 操作なしの時間
    prev = None
    print(f"{total:.0f} 秒測る(キーボード・マウスに触らない)", flush=True)
    while time.time() - t0 < total:
        on = bool(u32.GetAsyncKeyState(VK_F24) & 0x8000)
        idle = idle_s()
        rows[on].append(idle)
        if on != prev:
            print(f"{time.time() - t0:6.1f}s  F24 {'押したまま(オン)' if on else 'なし(オフ)'}", flush=True)
            prev = on
        if int((time.time() - t0) * 2) % 10 == 0:   # 5 秒おき
            print(f"{time.time() - t0:6.1f}s  F24={'オン' if on else 'オフ'}  操作なしの時間 {idle:5.1f} 秒", flush=True)
        time.sleep(0.5)
    for on, v in rows.items():
        if v:
            print(f"F24 {'オン' if on else 'オフ'} の間: {len(v)} 回測定・操作なしの時間の最大 {max(v):.1f} 秒", flush=True)


if __name__ == "__main__":
    main()
